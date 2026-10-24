<script setup>
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import api, { humanizeError } from '../api'

// ── 按病查药 ──────────────────────────────────────────────────
const diseaseQ = ref('')
const diseaseHits = ref([])
const picked = ref('')
const drugs = ref(null)          // DiseaseDrugs 响应
const busyD = ref(false)

async function searchDisease() {
  const q = diseaseQ.value.trim()
  if (!q) return
  busyD.value = true
  try {
    diseaseHits.value = (await api.get('/query/diseases', { params: { q } })).data
    if (!diseaseHits.value.length) ElMessage.info('图谱里没有以这个前缀开头的疾病')
  } catch (e) {
    ElMessage.error('疾病检索失败：' + humanizeError(e))
  } finally {
    busyD.value = false
  }
}

async function loadDrugs(name) {
  picked.value = name
  drugs.value = null
  try {
    drugs.value = (await api.get('/query/disease-drugs',
                                 { params: { name } })).data
  } catch (e) {
    ElMessage.error('查询推荐用药失败：' + humanizeError(e))
  }
}

// ── 两两相互作用 ──────────────────────────────────────────────
const drugA = ref('')
const drugB = ref('')
const optionsA = ref([])
const optionsB = ref([])
const inter = ref(null)
const busyI = ref(false)

async function remoteSearch(q, target) {
  if (!q || q.length < 1) { target.value = []; return }
  try {
    const { data } = await api.get('/drugs/search', { params: { q } })
    const items = data.items.map((x) => ({ value: x.name, label: x.name }))
    target.value = items
  } catch {
    target.value = []
  }
}

async function checkInteraction() {
  if (!drugA.value || !drugB.value) return
  busyI.value = true
  inter.value = null
  try {
    inter.value = (await api.get('/query/interaction',
                                 { params: { drug_a: drugA.value, drug_b: drugB.value } })).data
  } catch (e) {
    ElMessage.error('相互作用查询失败：' + humanizeError(e))
  } finally {
    busyI.value = false
  }
}

//: 严重程度 → 标签颜色。**未收录不用绿色**（绿色＝安全，而我们并不知道安全）。
const LEVEL_TAG = { Major: 'danger', Moderate: 'warning', Minor: 'info',
                    Unknown: 'warning' }
</script>

<template>
  <div>
    <header class="ph-page">
      <h1>医药查询</h1>
      <p>按病查推荐用药，或查两两药物之间的相互作用。</p>
    </header>
    <el-card shadow="never" style="margin-bottom:14px">
      <template #header>① 按病查推荐用药</template>
      <el-input v-model="diseaseQ" placeholder="疾病名前缀，如 类风湿"
                data-test="disease-q" @keyup.enter="searchDisease">
        <template #append>
          <el-button :loading="busyD" data-test="disease-search"
                     @click="searchDisease">检索</el-button>
        </template>
      </el-input>

      <div v-if="diseaseHits.length" style="margin-top:10px" data-test="disease-hits">
        <div style="font-size:12px;color:#909399;margin-bottom:6px">
          命中 {{ diseaseHits.length }} 个疾病（括号里是图里该病的推荐用药条数）
        </div>
        <el-button v-for="d in diseaseHits" :key="d.name" size="small"
                   style="margin:4px" :type="picked === d.name ? 'primary' : ''"
                   @click="loadDrugs(d.name)">
          {{ d.name }}（{{ d.drug_count }}）
        </el-button>
      </div>

      <div v-if="drugs" style="margin-top:14px" data-test="disease-result">
        <div style="font-size:13px;margin-bottom:6px">
          <b>{{ drugs.disease }}</b> 的推荐用药共 <b>{{ drugs.total }}</b> 条
          <span v-if="drugs.truncated" style="color:#b88230">
            （结果被截断，只显示 {{ drugs.drugs.length }} 条）
          </span>
        </div>
        <el-table :data="drugs.drugs" size="small" max-height="320">
          <el-table-column prop="name" label="药物" />
          <el-table-column prop="source" label="来源" width="120" />
        </el-table>
        <el-alert type="warning" :closable="false" style="margin-top:8px"
                  :title="drugs.disclaimer" />
      </div>
    </el-card>

    <el-card shadow="never">
      <template #header>② 两味药之间有相互作用吗</template>
      <el-row :gutter="8">
        <el-col :span="10">
          <el-select v-model="drugA" filterable remote clearable
                     placeholder="第一味药（输入前缀搜索）" style="width:100%"
                     :remote-method="(q) => remoteSearch(q, optionsA)"
                     data-test="drug-a">
            <el-option v-for="o in optionsA" :key="o.value" :label="o.label"
                       :value="o.value" />
          </el-select>
        </el-col>
        <el-col :span="10">
          <el-select v-model="drugB" filterable remote clearable
                     placeholder="第二味药" style="width:100%"
                     :remote-method="(q) => remoteSearch(q, optionsB)"
                     data-test="drug-b">
            <el-option v-for="o in optionsB" :key="o.value" :label="o.label"
                       :value="o.value" />
          </el-select>
        </el-col>
        <el-col :span="4">
          <el-button type="primary" :loading="busyI" style="width:100%"
                     data-test="check-interaction"
                     @click="checkInteraction">查询</el-button>
        </el-col>
      </el-row>

      <div v-if="inter" style="margin-top:14px" data-test="interaction-result">
        <div style="font-size:15px;margin-bottom:6px">
          <b>{{ inter.drug_a }}</b> ↔ <b>{{ inter.drug_b }}</b>
          <el-tag v-if="inter.found" :type="LEVEL_TAG[inter.level] || 'info'"
                  style="margin-left:8px" data-test="level-tag">
            {{ inter.level_label }}（{{ inter.level }}）
          </el-tag>
          <!-- ⚠️ 未收录用**灰色**。绿色在中文 UI 里＝安全＝可以同服，
               而"图谱没收录"和"没有相互作用"在这里根本分不开。 -->
          <el-tag v-else type="info" effect="plain" style="margin-left:8px"
                  data-test="level-tag">{{ inter.level_label }}</el-tag>
        </div>
        <div v-if="inter.note" style="color:#909399;font-size:12px">
          {{ inter.note }}
        </div>
        <el-alert v-if="inter.risk_hint" type="warning" :closable="false"
                  style="margin-top:8px" data-test="risk-hint"
                  :title="inter.risk_hint" />
        <div style="color:#606266;font-size:12px;margin-top:8px">
          来源：{{ inter.source || '—' }}<br>
          {{ inter.coverage_note }}
        </div>
        <el-alert type="error" :closable="false" style="margin-top:8px"
                  :title="inter.disclaimer" />
      </div>
    </el-card>
  </div>
</template>
