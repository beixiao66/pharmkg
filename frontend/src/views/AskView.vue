<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import api, { humanizeError } from '../api'
import { isDirected, levelLabel, typeLabel } from '../labels'
import EvidenceGraph from '../components/EvidenceGraph.vue'

//: 一轮一个对象：{question, answer, meta}
//: meta 里存接口返回的全部字段（意图、实体、Cypher、证据、忠实度…），
//: 用于「展开看依据」——提案里承诺"每条回答都能展开看实际执行的 Cypher"。
const turns = ref([])
const q = ref('')
const busy = ref(false)

// ── 会话持久化（L7 的验收就是「刷新/重登不丢历史」）──────────────
//: ⚠️ 第一版问答页**完全没有接持久化**，`turns` 只活在内存里——刷新就丢。
//:    而 L7 的验收标准写的是「刷新/重登不丢历史」，那条此前只在 API 层测过
//:    （`l7_selftest.py`），**用户界面上并未兑现**。这里补上。
const convId = ref(null)
const convs = ref([])

//: 送给后端的多轮历史：**只含有答案的轮次**（失败/拒答的不算上下文）。
const history = computed(() =>
  turns.value.filter((t) => t.answer)
    .map((t) => ({ question: t.question, answer: t.answer })))

async function refreshConversations() {
  try {
    convs.value = (await api.get('/conversations')).data
  } catch {
    convs.value = []          // 列表拿不到不该阻塞提问
  }
}

/** 把扁平的 messages 还原成 turns。
 *
 *  ⚠️ 消息表只持久化了 `rewritten_query / cypher / evidence`，
 *     **没有** `intent / entities / row_count`。所以恢复出来的轮次
 *     只显示答案 + 改写 + Cypher + 证据子图，**不显示那三个标签**——
 *     宁可少显示，也不要在刷新后编造它们。
 */
async function loadConversation(id) {
  convId.value = id
  try {
    // ⚠️ **必须显式传 limit**。后端默认值曾经是 6（= 3 轮），前端不带参数就
    //    只拿到最后 3 轮 —— 用户的第 4 轮之前**凭空消失**，界面上没有任何提示
    //    （会话按钮显示「(18)」，点开只有 3 轮，自相矛盾）。
    //    这里显式要 200 条（= 100 轮），真的被截断时也会明确告诉用户。
    const { data } = await api.get(`/conversations/${id}/messages`,
                                   { params: { limit: 200 } })
    const out = []
    for (const m of data) {
      if (m.role === 'user') {
        out.push({ question: m.content, answer: '', meta: null })
      } else {
        const last = out[out.length - 1]
        if (!last) continue
        last.answer = m.content
        last.meta = {
          rewritten: m.rewritten_query || '',
          cypher: m.cypher,
          evidence: m.evidence,
          restored: true,     // 标记"这是从库里恢复的"，模板据此少显示几个标签
        }
      }
    }
    turns.value = out
    // 拿到 200 条说明后面可能还有——明说，别让用户以为历史就这么多
    if (data.length >= 200) {
      ElMessage.warning('这个会话很长，只加载了最近 200 条消息')
    }
  } catch (e) {
    ElMessage.error('加载会话历史失败：' + humanizeError(e))
    turns.value = []
  }
}

async function newConversation() {
  try {
    const { data } = await api.post('/conversations', { title: '' })
    convId.value = data.id
    turns.value = []
    await refreshConversations()
  } catch (e) {
    ElMessage.error('新建会话失败：' + humanizeError(e))
  }
}

onMounted(async () => {
  await refreshConversations()
  // 默认打开最近一个会话——用户刷新页面最想要的就是"接着刚才继续"
  if (convs.value.length) await loadConversation(convs.value[0].id)
})

/** 落库一轮对话（失败不影响已算出的答案）。 */
async function persist(text, data) {
  try {
    if (!convId.value) {
      const { data: c } = await api.post('/conversations', { title: '' })
      convId.value = c.id
    }
    await api.post(`/conversations/${convId.value}/messages`,
                   { role: 'user', content: text })
    await api.post(`/conversations/${convId.value}/messages`, {
      role: 'assistant', content: data.answer,
      rewritten_query: data.rewritten, cypher: data.cypher,
      evidence: data.evidence,
    })
    await refreshConversations()
  } catch {
    // 写库失败不该影响"答案已经算出来"这件事（/ask 与 /conversations 是分开的接口）
  }
}

async function send() {
  const text = q.value.trim()
  if (!text || busy.value) return
  busy.value = true
  try {
    // ⚠️ 这里**不用**捕获后静默——`/ask` 可能因为 LLM 未配置(503)、索引未建、
    //    查询失败(500) 而报错，用户必须看到原因，否则就是"点了没反应"。
    const { data } = await api.post('/ask', {
      question: text, history: history.value,
    })
    turns.value.push({ question: text, answer: data.answer, meta: data })
    q.value = ''
    await persist(text, data)      // 落库（失败静默，不打断体验）
  } catch (e) {
    ElMessage.error('提问失败：' + humanizeError(e))
  } finally {
    busy.value = false
  }
}

/** 把 `/ask` 返回的证据（原始图结构）适配成 EvidenceGraph 要的形状。
 *
 *  ⚠️ 边标签要翻成人话：证据里的 `type` 是关系类型的英文名
 *  （`INTERACTS_WITH`），用户实测截图里边上就显示着这串大写——
 *  那是给开发者看的。有 `level` 属性时优先显示「相互作用（高风险）」这种。
 */
function asGraph(ev) {
  if (!ev || !ev.nodes) return { nodes: [], edges: [] }
  const nodes = (ev.nodes || []).filter((n) => n.name).map((n) => ({
    id: n.name,
    label: n.name,
    type: n.label || (n.labels && n.labels[0]) || 'Node',
  }))
  const edges = (ev.edges || []).filter((e) => e.from && e.to).map((e) => {
    const t = typeLabel(e.type)
    const lv = levelLabel((e.props || {}).level)
    return {
      source: e.from, target: e.to,
      label: lv ? `${t}（${lv}）` : t,
      // 无向关系（相互作用）不画箭头；有方向的（禁忌/推荐用药）才画
      directed: isDirected(e.type),
    }
  })
  return { nodes, edges }
}

const STEP_HINT = {
  ok: '',
  澄清: '图谱里这条名字对应多个条目，所以先回问而不是猜。',
  无实体: '没能从问题里认出图谱中的实体——按"无据拒答"处理，不用常识硬答。',
  白名单未过: '生成的查询没通过只读校验，已被拦下。',
  执行失败: '查询执行出错，已中止（不会拿不完整的结果凑答案）。',
  超条数上限: '查询返回行数超过上限，已拒绝以免拖垮服务。',
}
</script>

<template>
  <div>
    <header class="ph-page">
      <h1>智能问答</h1>
      <p>用中文问一句；只在图谱有据可查时作答，并给出依据。</p>
    </header>
    <!-- 会话栏：L7 的验收是「刷新/重登不丢历史」，所以这里必须能切会话 -->
    <el-card shadow="never" style="margin-bottom:12px">
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <el-button size="small" type="primary" data-test="new-conv"
                   @click="newConversation">新对话</el-button>
        <span style="font-size:12px;color:#909399">最近会话：</span>
        <el-button v-for="c in convs.slice(0, 6)" :key="c.id" size="small"
                   :type="convId === c.id ? 'primary' : ''" plain
                   :data-test="`conv-${c.id}`" @click="loadConversation(c.id)">
          {{ c.title || `会话 ${c.id}` }}（{{ c.message_count }}）
        </el-button>
        <span v-if="!convs.length" style="font-size:12px;color:#c0c4cc">
          还没有历史会话
        </span>
      </div>
    </el-card>

    <el-card v-if="!turns.length" shadow="never">
      <template #header>使用说明</template>
      <p style="color:#606266;font-size:13px">
        直接问一句话，系统会走：<b>改写 → 意图 → 实体链接 → 生成 Cypher →
        只读校验 → 执行 → 证据 → 答案</b>。答案只依据图谱事实；查不到会明确拒答。
      </p>
      <p style="color:#909399;font-size:12px">
        试试：卡维地洛和依度沙班能一起吃吗？／类风湿关节炎可以用哪些药？／
        骨肽片是治什么的？
      </p>
    </el-card>

    <div v-for="(t, i) in turns" :key="i" style="margin-bottom:14px">
      <el-card shadow="never" :data-test="`turn-${i}`">
        <div style="font-weight:600;margin-bottom:6px">
          <el-tag size="small" type="info">问</el-tag> {{ t.question }}
        </div>
        <div style="white-space:pre-wrap" :data-test="`answer-${i}`">{{ t.answer }}</div>

        <!-- 多轮改写：改写后和原问题不同才显示，否则是噪音 -->
        <div v-if="t.meta.rewritten && t.meta.rewritten !== t.question"
             style="color:#909399;font-size:12px;margin-top:6px"
             data-test="rewritten">
          改写为：{{ t.meta.rewritten }}
        </div>

        <div v-if="t.meta && !t.meta.restored" style="margin-top:8px;font-size:12px;color:#606266">
          <el-tag size="small">意图：{{ t.meta.intent || '—' }}</el-tag>
          <el-tag v-for="e in t.meta.entities" :key="e" size="small" type="success"
                  style="margin-left:6px">{{ e }}</el-tag>
          <el-tag size="small" type="warning" style="margin-left:6px"
                  data-test="row-count">命中 {{ t.meta.row_count }} 行</el-tag>
          <el-tag v-if="t.meta.faithful === false" size="small" type="danger"
                  style="margin-left:6px">⚠️ 忠实度检查未过</el-tag>
        </div>
        <!-- 从库里恢复的轮次没有 intent/entities/row_count（未持久化），如实说明 -->
        <div v-else-if="t.meta" style="margin-top:8px;font-size:12px;color:#c0c4cc">
          历史记录（意图/实体/命中行数未持久化，故不显示）
        </div>

        <el-alert v-if="STEP_HINT[t.meta.step]" type="info" :closable="false"
                  style="margin-top:8px" :title="STEP_HINT[t.meta.step]" />

        <!-- 可解释性：Cypher 与结果都在折叠里 -->
        <el-collapse style="margin-top:8px">
          <el-collapse-item title="查看依据（实际执行的 Cypher 与命中结果）"
                            :name="'d' + i" :data-test="`details-${i}`">
            <div style="font-size:12px">
              <div style="color:#606266;margin-bottom:4px">实际执行的 Cypher：</div>
              <pre data-test="cypher"
                   style="background:#f5f7fa;padding:8px;border-radius:4px;overflow:auto;font-size:12px">{{ t.meta.cypher || '（未生成）' }}</pre>
              <div style="color:#606266;margin:8px 0 4px">
                命中结果（共 {{ t.meta.row_count }} 行，最多显示 20 行）：
              </div>
              <pre style="background:#f5f7fa;padding:8px;border-radius:4px;overflow:auto;font-size:12px">{{ JSON.stringify(t.meta.rows_preview, null, 1) }}</pre>
            </div>
          </el-collapse-item>
        </el-collapse>

        <div v-if="t.meta.evidence" style="margin-top:10px">
          <EvidenceGraph :graph="asGraph(t.meta.evidence)" />
        </div>
      </el-card>
    </div>

    <el-card shadow="never" style="position:sticky;bottom:0">
      <el-input v-model="q" placeholder="问一句，例如：卡维地洛和依度沙班能一起吃吗？"
                data-test="ask-q" :disabled="busy" @keyup.enter="send">
        <template #append>
          <el-button type="primary" :loading="busy" data-test="ask-send"
                     @click="send">提问</el-button>
        </template>
      </el-input>
      <p style="color:#909399;font-size:12px;margin:6px 0 0">
        多轮：接着问「它和依度沙班能一起吃吗？」——系统会把「它」补成上文提到的药名。
        图谱数据仅供参考，不能替代医生或药师的判断。
      </p>
    </el-card>
  </div>
</template>
