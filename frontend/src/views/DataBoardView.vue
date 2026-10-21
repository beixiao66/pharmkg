<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import api, { humanizeError } from '../api'

const q = ref(null)
const busy = ref(false)

async function load() {
  busy.value = true
  try {
    q.value = (await api.get('/quality/graph')).data
  } catch (e) {
    ElMessage.error('加载质量数据失败：' + humanizeError(e))
  } finally {
    busy.value = false
  }
}
onMounted(load)

const scale = computed(() => q.value?.scale || {})
const labels = computed(() =>
  Object.entries(scale.value.nodes_by_label || {})
    .filter(([, v]) => v > 0).sort((a, b) => b[1] - a[1]))
const rels = computed(() =>
  Object.entries(scale.value.edges_by_type || {})
    .sort((a, b) => b[1] - a[1]).slice(0, 10))

//: ⚠️ 「判定」与「能否清洗」这两列是**人工判断**，来自 docs/图谱质量报告.md，
//: 不是脚本算出来的。脚本只给数字——**数字不会告诉你该不该删**。
//: 把判断和数字并排放在一起，是为了让看的人分清哪部分是事实、哪部分是结论。
const VERDICTS = [
  { key: 'self_loop', name: '自环', verdict: '确定的图论错误', can: '✅ 可以',
    why: '一个疾病不可能是自己的伴随疾病（上游 OpenCMKG 的缺陷）' },
  { key: 'dosage_split', name: '剂型分裂', verdict: '数据固有形状，不该合并',
    can: '❌ 不该清洗',
    why: '每个剂型都是独立注册药品；要的是实体链接归一化，不是合并节点' },
  { key: 'junk_concat', name: '拼接垃圾 · 截断碎片', verdict: '真垃圾',
    can: '✅ 可以', why: '18AA-Ⅰ / Ⅰ / SP 这类碎片被当成了药' },
  { key: 'junk_concat', name: '拼接垃圾 · 超长 ≥12 字', verdict: '⚠️ 精度低',
    can: '❌ 判据要重写',
    why: '多数是合法疫苗株名（甲型流感病毒A/越南/1194/2004(H5N1)株抗原…）；' +
         '少数是「整理者的注释混进了名字」' },
  { key: 'person_noise', name: '人名噪声', verdict: '实测为 0，该靶点不成立',
    can: '—', why: '带 医生/教授/氏 标记的一个都没有（是 0，不是"未发现"）' },
  { key: 'oov', name: 'OOV 实体', verdict: 'schema 与抽取目标未对齐',
    can: '❌ 要改的是槽位定义',
    why: '样本里有 基因型/检查指标/过敏史 —— 既不是疾病也不是人群' },
]

function num(key, field) {
  const v = q.value?.[key]
  return v && typeof v === 'object' ? (v[field] ?? 0) : 0
}
const junkFragment = computed(() => num('junk_concat', 'fragment_count'))
const junkLong = computed(() => num('junk_concat', 'too_long_count'))
</script>

<template>
  <div v-loading="busy">
    <el-card v-if="q" shadow="never" style="margin-bottom:14px">
      <template #header>图谱规模</template>
      <el-row :gutter="12">
        <el-col :span="6"><el-statistic title="节点总数"
          :value="scale.nodes_total || 0" /></el-col>
        <el-col :span="6"><el-statistic title="关系总数"
          :value="scale.edges_total || 0" /></el-col>
        <el-col :span="12">
          <div style="font-size:12px;color:#909399;line-height:1.7">
            快照生成于 <b>{{ (q._meta && q._meta.generated_at) || '未知' }}</b><br>
            {{ (q._meta && q._meta.note) || '' }}
          </div>
        </el-col>
      </el-row>
      <el-alert type="warning" :closable="false" style="margin-top:10px"
        title="这是快照，不是实时统计。图谱重新导入后必须重跑 scripts/check_graph_quality.py，否则看板显示的是旧图的质量。" />
    </el-card>

    <el-row v-if="q" :gutter="14">
      <el-col :span="11">
        <el-card shadow="never"><template #header>实体分布（按标签）</template>
          <el-table :data="labels" size="small" data-test="label-table">
            <el-table-column prop="0" label="标签" />
            <el-table-column prop="1" label="数量" align="right" />
          </el-table>
        </el-card>
      </el-col>
      <el-col :span="13">
        <el-card shadow="never"><template #header>关系分布（前 10 类）</template>
          <el-table :data="rels" size="small">
            <el-table-column prop="0" label="关系类型" />
            <el-table-column prop="1" label="数量" align="right" />
          </el-table>
        </el-card>
      </el-col>
    </el-row>

    <el-card v-if="q" shadow="never" style="margin-top:14px">
      <template #header>
        五个靶点（<span style="color:#c0392b">数字是脚本算的，判定是人下的</span>）
      </template>
      <el-table :data="VERDICTS" size="small" data-test="verdict-table">
        <el-table-column prop="name" label="靶点" width="150" />
        <el-table-column label="实测" width="150">
          <template #default="{ row }">
            <span v-if="row.name === '自环'">{{ num('self_loop', 'count') }}</span>
            <span v-else-if="row.name === '剂型分裂'">
              {{ num('dosage_split', 'split_bases') }} 主成分 /
              {{ num('dosage_split', 'split_drugs') }} 药
            </span>
            <span v-else-if="row.name.includes('截断')">{{ junkFragment }}</span>
            <span v-else-if="row.name.includes('超长')">{{ junkLong }}</span>
            <span v-else-if="row.name === '人名噪声'">
              {{ num('person_noise', 'drug_marked_count') }} /
              {{ num('person_noise', 'disease_marked_count') }}
            </span>
            <span v-else-if="row.name === 'OOV 实体'">{{ num('oov', 'oov_count') }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="verdict" label="判定" width="200" />
        <el-table-column prop="can" label="能否直接用于清洗" width="150" />
        <el-table-column prop="why" label="依据" />
      </el-table>
      <p style="color:#909399;font-size:12px;margin:8px 0 0">
        详见 <code>docs/图谱质量报告.md</code>。完整清单（含每个靶点的样例）在下面。
      </p>
    </el-card>

    <el-card v-if="q" shadow="never" style="margin-top:14px">
      <template #header>样例（判断"脏成什么样"靠这个，不靠数字）</template>
      <el-collapse data-test="samples">
        <el-collapse-item title="自环" name="a">
          <pre class="samp">{{ JSON.stringify(q.self_loop && q.self_loop.samples, null, 1) }}</pre>
        </el-collapse-item>
        <el-collapse-item title="剂型分裂" name="b">
          <pre class="samp">{{ JSON.stringify(q.dosage_split && q.dosage_split.samples, null, 1) }}</pre>
        </el-collapse-item>
        <el-collapse-item title="拼接垃圾（截断碎片 / 超长）" name="c">
          <pre class="samp">{{ JSON.stringify(q.junk_concat, null, 1) }}</pre>
        </el-collapse-item>
        <el-collapse-item title="人名噪声 + 2–3 字短名抽样" name="d">
          <pre class="samp">{{ JSON.stringify(q.person_noise, null, 1) }}</pre>
        </el-collapse-item>
        <el-collapse-item title="OOV 实体（schema 与抽取目标未对齐的证据）" name="e">
          <pre class="samp">{{ JSON.stringify(q.oov, null, 1) }}</pre>
        </el-collapse-item>
      </el-collapse>
    </el-card>
  </div>
</template>

<style scoped>
.samp {
  background: #f5f7fa;
  padding: 8px;
  border-radius: 4px;
  font-size: 12px;
  max-height: 260px;
  overflow: auto;
  margin: 0;
}
</style>
