<script setup>
import { computed, nextTick, onMounted, ref, watch } from 'vue'
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

//: 空态里给的三个示例。点一下就发出去——比让用户自己编一个问题容易得多，
//: 也顺带把"这个系统能问什么"演示了。
const EXAMPLES = [
  '卡维地洛和依度沙班能一起吃吗？',
  '类风湿关节炎可以用哪些药？',
  '骨肽片是治什么的？',
]
/** 消息区容器：新消息要自动滚到底部，否则用户以为没反应。 */
const scroller = ref(null)

function useExample(text) {
  q.value = text
  send()
}

// 轮次变化（发送成功 / 载入历史）后自动滚到底部
watch(turns, async () => {
  await nextTick()
  const el = scroller.value
  if (el) el.scrollTop = el.scrollHeight
}, { deep: true })

// ── 会话持久化（L7 的验收就是「刷新/重登不丢历史」）──────────────
//: ⚠️ 第一版问答页**完全没有接持久化**，`turns` 只活在内存里——刷新就丢。
//:    而 L7 的验收标准写的是「刷新/重登不丢历史」，那条此前只在 API 层测过
//:    （`l7_selftest.py`），**用户界面上并未兑现**。这里补上。
const convId = ref(null)
const convs = ref([])

//: 送给后端的多轮历史：**只含有答案的轮次**（失败/拒答的不算上下文）。
//:
//: ⚠️ **必须截断**。后端 `AskRequest.history` 有 `max_length=10` 的硬上限，
//:    而这里原来把**全部**轮次都发过去——聊到第 11 轮时每次提问都返回 422，
//:    界面上就是一句「提问失败：history: List should have at most 10 items…」。
//:
//:    为什么是 6 而不是 10：后端只在**改写**那一步用 `history[-3:]`（§6.2 的上下文窗口），
//:    发满 10 条纯属浪费 token；6 条给 3 轮窗口留了足够冗余，也给"以后窗口调大"留了空间。
//:    **截断在前端做，后端仍保留 10 的防守上限**（接口不该假设调用方守规矩）。
const HISTORY_TURNS = 6
const history = computed(() =>
  turns.value.filter((t) => t.answer)
    .slice(-HISTORY_TURNS)
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
  <div class="ask">
    <header class="ph-page">
      <h1>智能问答</h1>
      <p>用中文问一句；只在图谱有据可查时作答，并给出依据。</p>
      <!-- 会话：放在标题右侧，不再单独占一张卡片 -->
      <div class="ph-page__aside ask__convs">
        <el-select v-model="convId" size="small" placeholder="最近会话"
                   class="ask__convSel" data-test="conv-select"
                   @change="loadConversation">
          <el-option v-for="c in convs" :key="c.id" :value="c.id"
                     :label="`${c.title || '会话 ' + c.id}（${c.message_count}）`" />
        </el-select>
        <el-button size="small" type="primary" plain data-test="new-conv"
                   @click="newConversation">新对话</el-button>
      </div>
    </header>

    <!-- 消息区：可滚动，向上生长 -->
    <div ref="scroller" class="ask__stream">
      <div v-if="!turns.length" class="ask__empty">
        <p class="ask__emptyTitle">问一句试试</p>
        <p class="ask__emptySub">
          系统只依据图谱作答——查不到会明确拒答，不会用常识硬编。
        </p>
        <div class="ask__examples">
          <button v-for="ex in EXAMPLES" :key="ex" type="button"
                  class="ask__example" @click="useExample(ex)">
            <span>{{ ex }}</span><b aria-hidden="true">→</b>
          </button>
        </div>
      </div>

      <div v-for="(t, i) in turns" :key="i" class="ask__turn" :data-test="`turn-${i}`">
        <div class="ask__q"><span>{{ t.question }}</span></div>

        <div class="ask__a">
          <div class="ask__text" :data-test="`answer-${i}`">{{ t.answer || '…' }}</div>

          <!-- 多轮改写：改写后和原问题不同才显示，否则是噪音 -->
          <p v-if="t.meta && t.meta.rewritten && t.meta.rewritten !== t.question"
             class="ask__rewritten" data-test="rewritten">
            改写为：{{ t.meta.rewritten }}
          </p>

          <div v-if="t.meta && !t.meta.restored" class="ask__tags">
            <el-tag size="small">意图：{{ t.meta.intent || '—' }}</el-tag>
            <el-tag v-for="e in t.meta.entities" :key="e" size="small" type="success">
              {{ e }}
            </el-tag>
            <el-tag size="small" type="warning" data-test="row-count">
              命中 {{ t.meta.row_count }} 行
            </el-tag>
            <el-tag v-if="t.meta.faithful === false" size="small" type="danger">
              ⚠️ 忠实度检查未过
            </el-tag>
          </div>
          <!-- 从库里恢复的轮次没有 intent/entities/row_count（未持久化），如实说明 -->
          <p v-else-if="t.meta" class="ask__restored">
            历史记录（意图/实体/命中行数未持久化，故不显示）
          </p>

          <el-alert v-if="t.meta && STEP_HINT[t.meta.step]" type="info" :closable="false"
                    class="ask__alert" :title="STEP_HINT[t.meta.step]" />

          <!-- 可解释性：Cypher 与结果都在折叠里 -->
          <el-collapse v-if="t.meta" class="ask__more">
            <el-collapse-item title="查看依据（实际执行的 Cypher 与命中结果）"
                              :name="'d' + i" :data-test="`details-${i}`">
              <div class="ask__code">
                <p>实际执行的 Cypher：</p>
                <pre data-test="cypher">{{ t.meta.cypher || '（未生成）' }}</pre>

                <!-- ⚠️ 恢复出来的轮次**没有** row_count / rows_preview（未入库）。
                     原来这里照旧渲染，于是显示成「命中结果（共  行…）」加一个空表格，
                     用户会以为是系统坏了。**宁可明说"没保存"，也不要留半截空壳。**
                     （为什么不去补存：那 20 行原始数据体积大，而答案已经是对它的解读；
                      更关键的是图谱若变了，会出现"旧行配新答案"的不一致。） -->
                <p v-if="t.meta.restored" class="ask__codeNote" data-test="rows-not-saved">
                  这一轮是从历史记录恢复的：「命中结果」当时没有入库，所以这里没有表格。
                  答案正文、Cypher 与证据子图都已保存。
                </p>
                <template v-else>
                  <p>命中结果（共 {{ t.meta.row_count }} 行，最多显示 20 行）：</p>
                  <pre>{{ JSON.stringify(t.meta.rows_preview, null, 1) }}</pre>
                </template>
              </div>
            </el-collapse-item>
          </el-collapse>

          <div v-if="t.meta && t.meta.evidence" class="ask__graph">
            <EvidenceGraph :graph="asGraph(t.meta.evidence)" :max-height="240" />
          </div>
        </div>
      </div>
    </div>

    <!-- 输入区：固定在底部（聊天界面的常规形态） -->
    <div class="ask__composer">
      <el-input v-model="q" type="textarea" resize="none"
                :autosize="{ minRows: 1, maxRows: 5 }"
                placeholder="问一句，例如：卡维地洛和依度沙班能一起吃吗？"
                data-test="ask-q" :disabled="busy"
                @keydown.enter.exact.prevent="send" />
      <div class="ask__composerRow">
        <span class="ph-muted">
          多轮：接着问「它和依度沙班能一起吃吗？」——系统会把「它」补成上文提到的药名。
          图谱数据仅供参考，不能替代医生或药师的判断。
        </span>
        <el-button type="primary" :loading="busy" data-test="ask-send"
                   @click="send">提问</el-button>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 聊天布局的骨架：
   标题（自动高）→ 消息区（flex:1，可滚）→ 输入区（自动高，贴底）。
   整体高度吃满内容区：减去 .main__inner 上下各 24px 的内边距。 */
.ask {
  display: flex;
  flex-direction: column;
  height: calc(100vh - var(--ph-s5) * 2);
  min-height: 460px;
}
.ask > .ph-page { flex: none; }

.ask__convs { display: flex; align-items: center; gap: var(--ph-s2); }
.ask__convSel { width: 220px; }

/* ── 消息区 ─────────────────────────────────────────────── */
.ask__stream {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  padding: var(--ph-s2) var(--ph-s1) var(--ph-s4);
  scroll-behavior: smooth;
}

/* 空态：欢迎语 + 三个示例卡，垂直居中 */
.ask__empty {
  height: 100%;
  display: flex; flex-direction: column;
  align-items: center; justify-content: center;
  text-align: center;
  padding: var(--ph-s5) var(--ph-s3);
}
.ask__emptyTitle {
  margin: 0 0 var(--ph-s2);
  font-size: 20px; font-weight: 700; color: var(--ph-ink);
}
.ask__emptySub {
  margin: 0 0 var(--ph-s5);
  max-width: 34em;
  font-size: 13px; line-height: 1.7; color: var(--ph-ink-2);
}
.ask__examples {
  display: grid; gap: var(--ph-s2);
  width: 100%; max-width: 460px;
}
.ask__example {
  appearance: none;
  display: flex; align-items: center; justify-content: space-between;
  gap: var(--ph-s3);
  padding: var(--ph-s3) var(--ph-s4);
  border: 1px solid var(--ph-line);
  border-radius: 10px;
  background: var(--ph-surface);
  color: var(--ph-ink);
  font-family: inherit; font-size: 13.5px;
  text-align: left; cursor: pointer;
  transition: border-color .16s ease, box-shadow .16s ease, transform .16s ease;
}
.ask__example:hover {
  border-color: var(--ph-brand);
  box-shadow: 0 4px 14px rgba(29, 78, 216, .10);
}
.ask__example b { color: var(--ph-brand); font-weight: 600; }

/* ── 一轮问答：问右、答左 ───────────────────────────────── */
.ask__turn { margin-bottom: var(--ph-s5); }
.ask__q { display: flex; justify-content: flex-end; margin-bottom: var(--ph-s3); }
.ask__q span {
  max-width: 78%;
  padding: 10px 14px;
  border-radius: 14px 14px 4px 14px;
  background: var(--ph-brand);
  color: #fff;
  font-size: 14px; line-height: 1.65;
}
.ask__a {
  max-width: 92%;
  padding: var(--ph-s4);
  border: 1px solid var(--ph-line);
  border-radius: 4px 14px 14px 14px;
  background: var(--ph-surface);
}
.ask__text {
  white-space: pre-wrap;
  font-size: 14px; line-height: 1.75;
  color: var(--ph-ink);
}
.ask__rewritten { margin: var(--ph-s2) 0 0; font-size: 12px; color: var(--ph-ink-3); }
.ask__restored { margin: var(--ph-s2) 0 0; font-size: 12px; color: var(--ph-ink-3); }
.ask__tags {
  display: flex; flex-wrap: wrap; gap: 6px;
  margin-top: var(--ph-s3);
}
.ask__alert { margin-top: var(--ph-s3); }
.ask__more { margin-top: var(--ph-s3); }
.ask__code { font-size: 12px; color: var(--ph-ink-2); }
.ask__code p { margin: var(--ph-s2) 0 4px; }
.ask__codeNote {
  margin: var(--ph-s3) 0 0;
  padding: var(--ph-s2) var(--ph-s3);
  border-radius: 8px;
  background: #f8fafc;
  border: 1px solid var(--ph-line);
  color: var(--ph-ink-2);
  line-height: 1.7;
}
.ask__code pre {
  margin: 0;
  padding: var(--ph-s2);
  border-radius: 6px;
  background: #f5f7fa;
  overflow: auto;
  font-size: 12px;
}
.ask__graph { margin-top: var(--ph-s3); }

/* ── 输入区 ─────────────────────────────────────────────── */
.ask__composer {
  flex: none;
  padding-top: var(--ph-s3);
  border-top: 1px solid var(--ph-line);
  background: var(--ph-page);
}
.ask__composerRow {
  display: flex; align-items: center; gap: var(--ph-s3);
  margin-top: var(--ph-s2);
}
.ask__composerRow .ph-muted { flex: 1 1 auto; }

@media (max-width: 900px) {
  .ask { height: calc(100vh - var(--ph-s4) * 2); }
  .ask__convSel { width: 150px; }
  .ask__a { max-width: 100%; }
  .ask__q span { max-width: 88%; }
  .ask__composerRow .ph-muted { display: none; }
}
</style>
