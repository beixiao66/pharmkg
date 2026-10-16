<script setup>
import { ref, onMounted, computed } from 'vue'
import { ElMessage } from 'element-plus'
import api from '../api'
import RiskCard from '../components/RiskCard.vue'
import EvidenceGraph from '../components/EvidenceGraph.vue'

const box = ref([])
const risk = ref(null)
const vocab = ref([])
const tags = ref([])
const q = ref('')
const hits = ref([])
const busy = ref(false)

async function load() {
  const [b, v] = await Promise.all([
    api.get('/medicine-box'), api.get('/medicine-box/population-vocabulary'),
  ])
  box.value = b.data
  vocab.value = v.data
  try {
    risk.value = (await api.get('/medicine-box/risk')).data
    tags.value = [...risk.value.population_tags]
  } catch (e) { ElMessage.error('风险检查失败：' + e.message) }
}

async function search() {
  if (!q.value.trim()) return
  hits.value = (await api.get('/drugs/search', { params: { q: q.value.trim() } })).data
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
    const d = e?.response?.data?.detail
    ElMessage.error(typeof d === 'string' ? d : e.message)
  } finally { busy.value = false }
}

async function remove(id) {
  await api.delete('/medicine-box/' + encodeURIComponent(id))
  await load()
}

async function saveTags() {
  try {
    await api.patch('/auth/me/population-tags', { tags: tags.value })
    ElMessage.success('人群标签已保存')
    await load()
  } catch (e) {
    const d = e?.response?.data?.detail
    ElMessage.error(typeof d === 'string' ? d : e.message)
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
          <template #append><el-button @click="search" data-test="drug-search">检索</el-button></template>
        </el-input>
        <div v-if="hits.length" style="margin-top:8px" data-test="search-hits">
          <el-button v-for="h in hits" :key="h.id" size="small" style="margin:4px"
                     :loading="busy" @click="add(h.id)">
            + {{ h.name }}
          </el-button>
        </div>
        <el-divider />
        <el-empty v-if="!box.length" description="药箱是空的" />
        <div v-for="b in box" :key="b.drug_id" style="margin-bottom:6px"
             :data-test="'box-' + b.drug_name">
          <el-tag closable @close="remove(b.drug_id)">{{ b.drug_name }}</el-tag>
        </div>
        <el-divider />
        <div style="font-size:13px;color:#606266;margin-bottom:6px">
          我的人群 / 过敏史（只列图谱里真有的）
        </div>
        <el-select v-model="tags" multiple size="small" style="width:100%"
                   data-test="tag-select">
          <el-option v-for="t in vocab" :key="t" :label="t" :value="t" />
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
