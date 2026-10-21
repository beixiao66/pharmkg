<script setup>
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import api, { humanizeError } from '../api'
import EvidenceGraph from '../components/EvidenceGraph.vue'

//: 一轮一个对象：{question, answer, meta}
//: meta 里存接口返回的全部字段（意图、实体、Cypher、证据、忠实度…），
//: 用于「展开看依据」——提案里承诺"每条回答都能展开看实际执行的 Cypher"。
const turns = ref([])
const q = ref('')
const busy = ref(false)

//: 送给后端的多轮历史：**只含有答案的轮次**（失败/拒答的不算上下文）。
const history = computed(() =>
  turns.value.filter((t) => t.answer)
    .map((t) => ({ question: t.question, answer: t.answer })))

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
  } catch (e) {
    ElMessage.error('提问失败：' + humanizeError(e))
  } finally {
    busy.value = false
  }
}

/** 把 `/ask` 返回的证据（原始图结构）适配成 EvidenceGraph 要的形状。 */
function asGraph(ev) {
  if (!ev || !ev.nodes) return { nodes: [], edges: [] }
  const nodes = (ev.nodes || []).filter((n) => n.name).map((n) => ({
    id: n.name,
    label: n.name,
    type: n.label || (n.labels && n.labels[0]) || 'Node',
  }))
  const edges = (ev.edges || []).filter((e) => e.from && e.to).map((e) => ({
    source: e.from, target: e.to, label: e.type || e.label || '',
  }))
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
    <el-card v-if="!turns.length" shadow="never">
      <template #header>智能问答</template>
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

        <div style="margin-top:8px;font-size:12px;color:#606266">
          <el-tag size="small">意图：{{ t.meta.intent || '—' }}</el-tag>
          <el-tag v-for="e in t.meta.entities" :key="e" size="small" type="success"
                  style="margin-left:6px">{{ e }}</el-tag>
          <el-tag size="small" type="warning" style="margin-left:6px"
                  data-test="row-count">命中 {{ t.meta.row_count }} 行</el-tag>
          <el-tag v-if="t.meta.faithful === false" size="small" type="danger"
                  style="margin-left:6px">⚠️ 忠实度检查未过</el-tag>
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
