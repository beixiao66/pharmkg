<script setup>
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import api, { humanizeError } from '../api'
import { isDirected, typeLabel } from '../labels'
import EvidenceGraph from '../components/EvidenceGraph.vue'

// ── 实体检索 + 邻域子图 ────────────────────────────────────────
const q = ref('')
const hits = ref([])
const center = ref('')
const sub = ref(null)
const busy = ref(false)

//: 示例入口：把「这页能查什么」直接摆出来。
const ENTITY_EXAMPLES = ['卡维地洛片', '类风湿关节炎', '孕妇']
const PATH_EXAMPLES = [['卡维地洛片', '孕妇'], ['骨肽片', '类风湿关节炎']]

async function useEntity(name) {
  q.value = name
  await searchEntity()
}

async function usePath(a, b) {
  src.value = a
  dst.value = b
  await findPath()
}

//: 同名的命中（同名不同标签）。必须解释一句，否则用户看到两个
//: 「类风湿关节炎」完全不知道为什么。
const dupNames = computed(() => {
  const cnt = {}
  for (const h of hits.value) cnt[h.name] = (cnt[h.name] || 0) + 1
  return Object.entries(cnt).filter(([, c]) => c > 1).map(([n]) => n)
})

async function searchEntity() {
  const text = q.value.trim()
  if (!text) return
  busy.value = true
  try {
    hits.value = (await api.get('/graph/entities', { params: { q: text } })).data
    if (!hits.value.length) ElMessage.info('图谱里没有以这个前缀开头的实体')
  } catch (e) {
    ElMessage.error('实体检索失败：' + humanizeError(e))
  } finally {
    busy.value = false
  }
}

async function loadSubgraph(name, label) {
  center.value = name
  sub.value = null
  try {
    sub.value = (await api.get('/graph/subgraph',
                               // ⚠️ **必须带 label**：图谱里有同名不同标签的节点（实测 1,774 个），
                               //   只传 name 的话后端 LIMIT 1 会随便挑一个，点「症状」却看到「疾病」。
                               { params: { name, label, limit: 120 } })).data
  } catch (e) {
    ElMessage.error('加载邻域失败：' + humanizeError(e))
  }
}

// ── 路径追溯 ──────────────────────────────────────────────────
const src = ref('')
const dst = ref('')
const hops = ref(4)
const path = ref(null)
const busyP = ref(false)

async function findPath() {
  if (!src.value || !dst.value) return
  busyP.value = true
  path.value = null
  try {
    path.value = (await api.get('/graph/path',
                                { params: { src: src.value, dst: dst.value, max_hops: hops.value } })).data
  } catch (e) {
    // ⚠️ 超时（504）与"没有路径"是两件事：这里会分别显示，不能混成一句"没找到"
    ElMessage.error('路径查询失败：' + humanizeError(e))
  } finally {
    busyP.value = false
  }
}

//: 把接口返回的 {nodes:[{id,label,type}], edges:[{source,target,label}]} 交给
//: EvidenceGraph。**边标签要翻译**——原始值是 `INTERACTS_WITH` 这类关系类型名，
//: 那是给开发者看的；给用户看的是「相互作用」。
function asGraph(d) {
  if (!d) return { nodes: [], edges: [] }
  return {
    nodes: d.nodes || [],
    edges: (d.edges || []).map((e) => ({
      ...e,
      label: typeLabel(e.label || e.type),
      directed: isDirected(e.type || e.label),
    })),
  }
}
</script>

<template>
  <div>
    <header class="ph-page">
      <h1>图谱浏览</h1>
      <p>检索实体、看邻域子图，或查两个实体之间的最短路径。</p>
    </header>
    <el-card shadow="never" style="margin-bottom:14px">
      <template #header>① 实体检索 → 邻域子图</template>
      <el-input v-model="q" placeholder="实体名前缀，如 卡维地洛 / 类风湿"
                data-test="entity-q" @keyup.enter="searchEntity">
        <template #append>
          <el-button :loading="busy" data-test="entity-search"
                     @click="searchEntity">检索</el-button>
        </template>
      </el-input>

      <div class="ph-examples">
        <span>试试：</span>
        <button v-for="ex in ENTITY_EXAMPLES" :key="ex" type="button"
                class="ph-chip" @click="useEntity(ex)">{{ ex }}</button>
      </div>

      <div v-if="hits.length" style="margin-top:10px" data-test="entity-hits">
        <div style="font-size:12px;color:#909399;margin-bottom:6px">
          命中 {{ hits.length }} 个（括号里是标签与连边数——孤点进去是空白页，所以按连边数排序）
        </div>

        <!-- 同名多标签：必须解释，否则用户看到两个一样的名字完全不知道为什么 -->
        <p v-if="dupNames.length" class="ph-hint" data-test="dup-hint">
          「{{ dupNames[0] }}」这个名字在图谱里对应<b>多个标签</b>
          （全图有 1,774 个这样的名字，属上游数据重复）。它们的连边完全不同——
          点哪个就看哪个：标成「疾病」的那个通常信息更多，另一类多是"作为症状被别的病引用"。
        </p>
        <el-button v-for="h in hits" :key="h.name + h.label" size="small"
                   style="margin:4px" :type="center === h.name ? 'primary' : ''"
                   @click="loadSubgraph(h.name, h.label)">
          {{ h.name }}（{{ typeLabel(h.label) }}，{{ h.degree }} 条边）
        </el-button>
      </div>

      <div v-if="sub" style="margin-top:14px" data-test="subgraph">
        <div style="font-size:13px;margin-bottom:6px">
          以 <b>{{ sub.center }}</b>（{{ typeLabel(sub.center_label) }}）为中心：
          <b>{{ sub.nodes.length }}</b> 个节点 / <b>{{ sub.edges.length }}</b> 条边
        </div>
        <el-alert v-if="sub.truncated" type="warning" :closable="false"
                  style="margin-bottom:8px" data-test="truncated"
                  :title="`只画了前 ${sub.edge_limit} 条边 —— 这个实体的连边更多，图上不是全部。`" />
        <EvidenceGraph :graph="asGraph(sub)" :max-nodes="10" />
      </div>
    </el-card>

    <el-card shadow="never">
      <template #header>② 两点之间的最短路径（路径追溯）</template>
      <el-row :gutter="8">
        <el-col :span="7">
          <el-input v-model="src" placeholder="起点实体" data-test="path-src" />
        </el-col>
        <el-col :span="7">
          <el-input v-model="dst" placeholder="终点实体" data-test="path-dst" />
        </el-col>
        <el-col :span="4">
          <el-select v-model="hops" style="width:100%" data-test="path-hops">
            <el-option v-for="n in [2, 3, 4, 5, 6]" :key="n"
                       :label="`最多 ${n} 跳`" :value="n" />
          </el-select>
        </el-col>
        <el-col :span="6">
          <el-button type="primary" :loading="busyP" style="width:100%"
                     data-test="path-find" @click="findPath">找路径</el-button>
        </el-col>
      </el-row>

      <div class="ph-examples">
        <span>试试：</span>
        <button v-for="p in PATH_EXAMPLES" :key="p[0] + p[1]" type="button"
                class="ph-chip" @click="usePath(p[0], p[1])">{{ p[0] }} → {{ p[1] }}</button>
      </div>
      <p style="color:#909399;font-size:12px;margin:6px 0 0">
        最多 6 跳（不设上限就是全图搜索）。查询超时会明确报错——超时不等于没有路径。
      </p>

      <div v-if="path" style="margin-top:12px" data-test="path-result">
        <div style="font-size:13px;margin-bottom:6px">
          <span v-if="path.found">
            <b>{{ path.hops }}</b> 跳：{{ path.nodes.map((n) => n.label).join(' → ') }}
          </span>
          <span v-else style="color:#b88230">{{ path.note }}</span>
        </div>
        <EvidenceGraph v-if="path.found" :graph="asGraph(path)" />
      </div>
    </el-card>
  </div>
</template>

<style scoped>

/* ── 示例入口：首屏只有输入框时，给出可以直接点的例子 ─────────── */
.ph-examples {
  display: flex; align-items: center; flex-wrap: wrap; gap: 6px;
  margin-top: var(--ph-s2);
}
.ph-examples span { font-size: 12px; color: var(--ph-ink-3); }
.ph-chip {
  appearance: none;
  padding: 4px 10px;
  border: 1px solid var(--ph-line);
  border-radius: 999px;
  background: var(--ph-surface);
  color: var(--ph-ink-2);
  font-family: inherit; font-size: 12.5px;
  cursor: pointer;
  transition: border-color .16s ease, color .16s ease, background .16s ease;
}
.ph-chip:hover {
  border-color: var(--ph-brand);
  color: var(--ph-brand);
  background: var(--ph-brand-soft);
}
</style>
