<script setup>
import { ref, onMounted, computed } from 'vue'
import { ElMessage } from 'element-plus'
import api, { humanizeError } from '../api'
import RiskCard from '../components/RiskCard.vue'
import EvidenceGraph from '../components/EvidenceGraph.vue'

const box = ref([])
const risk = ref(null)
const vocab = ref([])
const tags = ref([])
//: 决策 #42：人群与过敏史是**两个字段**（过敏史不是「人群」，原来混在一起
//: 逼得界面只能叫「人群 / 过敏史」来打补丁）。
const allergyTags = ref([])
const q = ref('')
const hits = ref([])
const busy = ref(false)
//: 检索的三态。**必须都记下来**，否则页面无法区分
//: 「没点」「没找到」「报错了」——而用户看到的都是"没反应"。
const searching = ref(false)
const searched = ref(false)
const lastQ = ref('')
//: 检索的总数与被截断标记（后端返回）。**截断必须显示出来**，见 search() 里的说明。
const meta = ref({ total: 0, truncated: false })

//: 词表按 group 拆开；**每个选项都显示图谱里有几条边**（0 就说"暂无数据"），
//: 否则用户会以为「标了就一定查得到」。
const popVocab = computed(() => vocab.value.filter((v) => v.group === 'population'))
const allergyVocab = computed(() => vocab.value.filter((v) => v.group === 'allergy'))
function tagLabel(v) {
  return v.mapped ? `${v.tag}（图谱 ${v.edges} 条）` : `${v.tag}（暂无数据）`
}

async function load() {
  const [b, v] = await Promise.all([
    api.get('/medicine-box'), api.get('/medicine-box/population-vocabulary'),
  ])
  box.value = b.data
  vocab.value = v.data
  try {
    risk.value = (await api.get('/medicine-box/risk')).data
    tags.value = [...(risk.value.population_tags || [])]
    allergyTags.value = [...(risk.value.allergy_history || [])]
  } catch (e) { ElMessage.error('风险检查失败：' + humanizeError(e)) }
}

async function search() {
  const qv = q.value.trim()
  if (!qv) return
  // ⚠️ 原来这里是**没有 try/catch 的**：一旦请求失败（后端没开、401、代理不通…），
  //    异常直接冒到控制台，页面上**什么都不显示** —— 用户的体验就是"点了没反应"。
  //    **静默失败比报错更难排查**，所以这里必须把结果和错误都摆到页面上。
  searching.value = true
  lastQ.value = qv
  try {
    // ⚠️ 后端现在返回 {items, total, limit, truncated}，**不是裸数组**——
    //    因为「被截断」必须能告诉用户（实测：以「卡」开头 29 个药，
    //    LIMIT 10 把卡维地洛片切掉了，页面却毫无提示）。
    const { data } = await api.get('/drugs/search', { params: { q: qv } })
    hits.value = data.items
    meta.value = { total: data.total, truncated: data.truncated }
    searched.value = true
  } catch (e) {
    hits.value = []
    meta.value = { total: 0, truncated: false }
    searched.value = true
    ElMessage.error('检索失败：' + humanizeError(e))
  } finally {
    searching.value = false
  }
}

async function add(id) {
  busy.value = true
  try {
    await api.post('/medicine-box', { drug_id: id })
    ElMessage.success('已加入药箱')
    hits.value = []
    q.value = ''
    await load()
  } catch (e) {
    // 同样**不要**把 detail 直接甩给用户：422 的 detail 是数组（见 src/api.js 的说明）
    ElMessage.error(humanizeError(e))
  } finally { busy.value = false }
}

async function remove(id) {
  await api.delete('/medicine-box/' + encodeURIComponent(id))
  await load()
}

async function saveTags() {
  try {
    await api.patch('/auth/me/population-tags',
                    { tags: tags.value, allergy_history: allergyTags.value })
    ElMessage.success('人群标签已保存')
    await load()
  } catch (e) {
    // ⚠️ 这里的 422 尤其重要：人群标签非法时后端返回的是**数组型** detail，
    //    里面写着"不支持的人群标签 [...]；可用的是 [...]"。第一版把它
    //    JSON.stringify 成一坨，用户完全看不懂。humanizeError 会把
    //    这类数组逐条翻成人话。
    ElMessage.error(humanizeError(e))
  }
}

onMounted(load)

// 由 risk 结果**在客户端拼出子图**：节点＝药箱里的药 + 命中的禁忌对象，
// 边＝有记录的相互作用 + 人群禁忌。这样不用为画图再开后端接口。
const graph = computed(() => {
  if (!risk.value) return { nodes: [], edges: [] }
  const nodes = new Map()
  const edges = []
  for (const b of box.value) nodes.set(b.drug_id, { id: b.drug_id, label: b.drug_name, type: 'Drug' })
  for (const p of risk.value.pairs) {
    if (!p.level) continue
    const a = [...nodes.values()].find((n) => n.label === p.drug_a)
    const b = [...nodes.values()].find((n) => n.label === p.drug_b)
    if (a && b) edges.push({ source: a.id, target: b.id, label: p.level })
  }
  for (const h of risk.value.population) {
    const d = [...nodes.values()].find((n) => n.label === h.drug)
    if (!d) continue
    const tid = 'pop:' + h.tag
    if (!nodes.has(tid)) nodes.set(tid, { id: tid, label: h.tag, type: 'Population' })
    edges.push({ source: d.id, target: tid, label: '禁忌' })
  }
  return { nodes: [...nodes.values()], edges }
})
</script>

<template>
  <el-row :gutter="16">
    <el-col :span="9">
      <el-card>
        <template #header>我的药箱（{{ box.length }}）</template>
        <el-input v-model="q" placeholder="输入药名前缀，如 卡维地洛" data-test="drug-q"
                  @keyup.enter="search">
          <template #append><el-button @click="search" :loading="searching"
                                       data-test="drug-search">检索</el-button></template>
        </el-input>
        <!-- ⚠️ 三种结果都必须有反馈。原来只在"有结果"时渲染，
             于是"点了没反应""没找到""请求报错"三种情况**在页面上长得一模一样**——
             用户没法判断，只能来问我。 -->
        <div v-if="hits.length" style="margin-top:8px" data-test="search-hits">
          <!-- ⚠️ 「被截断」必须说出来。实测：以「卡」开头 29 个药，
               原来 LIMIT 10 把「卡维地洛片」（第 17 位）切掉了，
               页面毫无提示 → 用户以为图谱里没有这个药。 -->
          <el-alert v-if="meta.truncated" type="warning" :closable="false"
                    style="margin-bottom:6px" data-test="search-truncated"
                    :title="`共 ${meta.total} 条，只显示了前 ${hits.length} 条 —— 请多输几个字缩小范围`" />
          <span v-else style="color:#909399;font-size:12px">
            共 {{ meta.total }} 条
          </span>
          <div style="margin-top:6px">
            <el-button v-for="h in hits" :key="h.id" size="small" style="margin:4px"
                       :loading="busy" @click="add(h.id)">
              + {{ h.name }}
            </el-button>
          </div>
        </div>
        <el-alert v-else-if="searched" type="info" :closable="false"
                  style="margin-top:8px" data-test="search-empty"
                  :title="`图谱里没有以「${lastQ}」开头的药物。换个前缀试试（如只输前两个字）。`" />
        <el-divider />
        <el-empty v-if="!box.length" description="药箱是空的" />
        <div v-for="b in box" :key="b.drug_id" style="margin-bottom:6px"
             :data-test="'box-' + b.drug_name">
          <el-tag closable @close="remove(b.drug_id)">{{ b.drug_name }}</el-tag>
        </div>
        <el-divider />
        <!-- 决策 #42：人群与过敏史**分开两个选择器**，且每个选项自带**图谱覆盖度**。
             覆盖度必须显示：`儿童`/`老年`/… 在图里一条边都没有，
             只说「标了就会查」会让用户得到"没查到风险"＝假的安全感。 -->
        <div style="font-size:13px;color:#606266;margin-bottom:4px">
          我的人群（括号里是该标签在图谱里的禁忌边数）
        </div>
        <el-select v-model="tags" multiple size="small" style="width:100%"
                   placeholder="如 妊娠期 / 哺乳期 / 儿童" data-test="tag-select">
          <el-option v-for="v in popVocab" :key="v.tag" :label="tagLabel(v)"
                     :value="v.tag" />
        </el-select>
        <div style="font-size:13px;color:#606266;margin:8px 0 4px">
          我的过敏史
        </div>
        <el-select v-model="allergyTags" multiple size="small" style="width:100%"
                   placeholder="如 大环内酯类药物过敏" data-test="allergy-select">
          <el-option v-for="v in allergyVocab" :key="v.tag" :label="tagLabel(v)"
                     :value="v.tag" />
        </el-select>
        <el-button size="small" type="primary" style="margin-top:8px"
                   @click="saveTags" data-test="save-tags">保存</el-button>
      </el-card>
    </el-col>
    <el-col :span="15">
      <el-card v-if="risk">
        <template #header>用药安全校验</template>
        <RiskCard :risk="risk" />
        <h3>④ 证据子图</h3>
        <EvidenceGraph :graph="graph" />
        <el-alert type="error" :closable="false" style="margin-top:12px"
                  :title="risk.disclaimer" />
        <p style="color:#909399;font-size:12px">{{ risk.attribution }}</p>
      </el-card>
      <el-card v-else><el-skeleton :rows="6" animated /></el-card>
    </el-col>
  </el-row>
</template>
