<script setup>
import { computed, onMounted, onBeforeUnmount, ref, watch } from 'vue'
import G6 from '@antv/g6'

// ⚠️ 这里钉的是 **G6 v4**（package.json 里 ^4.8）。v5 的 API 完全不同
//    （`new Graph({data, node, edge})` 那一套），不能混用。
//
// ## 这一版是第二轮重做，起因同样是用户截图
//
// 第一轮解决了：布局退化（无 layout → 摆成一条线）、箭头语义、标签溢出。
// 第二轮解决截图暴露的两件事：
//
// 1. **边标签压在节点上**：G6 默认把边标签放在**线段中点**。两点相距不远的图上，
//    标签（「相互作用（高风险）」有 11 个字）比线段还长，直接盖住两侧节点。
//    → `labelCfg.refY: -10` 把标签**垂直于线**推出去，不再压节点。
// 2. **边一多，标签就成一团糊**：24 条边的「推荐用药」标签全旋转着挤在中心。
//    → 边数超过 `MAX_LABELED_EDGES` 时**不画边标签**，改由下方的文字说明承担
//      （"关系均为「推荐用药」"）——**图形放弃的细节，用文字补回来**。
//
// 另外按用户要求把节点上限从 30 降到 **15**：宁可少画、看清楚，
// 也不要把 30 个塞满。少画的部分在文字里说清数量与去处。
const props = defineProps({
  graph: { type: Object, required: true },
  /** 最多画多少个节点。超过就按度数取前 N 个，并在图下注明。 */
  maxNodes: { type: Number, default: 15 },
})

const box = ref(null)
const note = ref('')
const summary = ref('')
let graph = null

const COLOR = {
  Drug: '#409eff',
  Disease: '#67c23a',
  Population: '#f0a020',
  Symptom: '#909399',
  default: '#b1b3b8',
}
const TYPE_CN = { Drug: '药物', Disease: '疾病', Population: '人群', Symptom: '症状' }
const LEVEL_COLOR = { Major: '#c0392b', Moderate: '#e67e22', Minor: '#95a5a6' }

const FONT = 11
const CHAR_W = 13
const NODE_H = 28
const MAX_LABEL = 12
//: 边数超过这个值就不画边标签——否则标签会挤成一团（实测 24 条边时完全不可读）
const MAX_LABELED_EDGES = 6

function shortLabel(s) {
  if (!s) return ''
  return s.length > MAX_LABEL ? s.slice(0, MAX_LABEL) + '…' : s
}

function nodeWidth(label) {
  return Math.min(180, Math.max(56, shortLabel(label).length * CHAR_W + 18))
}

/** 按度数取前 maxNodes 个节点；返回裁剪后的图、"是否截断"、以及类型统计。 */
function pick(src) {
  const all = (src.nodes || []).filter((n) => n && n.id)
  const deg = new Map(all.map((n) => [n.id, 0]))
  for (const e of src.edges || []) {
    if (deg.has(e.source)) deg.set(e.source, deg.get(e.source) + 1)
    if (deg.has(e.target)) deg.set(e.target, deg.get(e.target) + 1)
  }
  const kept = all.length <= props.maxNodes
    ? all
    : [...all].sort((a, b) => (deg.get(b.id) || 0) - (deg.get(a.id) || 0))
        .slice(0, props.maxNodes)
  const ids = new Set(kept.map((n) => n.id))
  const edges = (src.edges || []).filter((e) => ids.has(e.source) && ids.has(e.target))
  return { nodes: kept, edges, total: all.length, truncated: kept.length < all.length }
}

/** 用一句话说清"图里是什么、少画了什么"——图形放弃的细节由文字承担。 */
function describe(keptNodes, keptEdges, total, truncated) {
  const byType = {}
  for (const n of keptNodes) {
    const t = TYPE_CN[n.type] || n.type || '实体'
    byType[t] = (byType[t] || 0) + 1
  }
  const typePart = Object.entries(byType).map(([k, v]) => `${v} 个${k}`).join('、')
  const kinds = [...new Set(keptEdges.map((e) => e.label).filter(Boolean))]
  const relPart = kinds.length
    ? `关系：${kinds.slice(0, 3).join('、')}${kinds.length > 3 ? ` 等 ${kinds.length} 种` : ''}`
    : '无连线'
  const cut = truncated
    ? `；另有 ${total - keptNodes.length} 个未画出，完整清单见上面的答案`
    : ''
  return `图中画了 ${keptNodes.length} 个节点（${typePart}），${keptEdges.length} 条连线（${relPart}）${cut}。`
}

function draw() {
  if (!box.value) return
  if (graph) { graph.destroy(); graph = null }

  const { nodes: rawNodes, edges: rawEdges, total, truncated } = pick(props.graph || {})
  const showEdgeLabels = rawEdges.length <= MAX_LABELED_EDGES

  summary.value = describe(rawNodes, rawEdges, total, truncated)
  note.value = truncated
    ? `节点过多，只画了关联最多的 ${rawNodes.length} 个（共 ${total} 个）。`
    : ''

  const nodes = rawNodes.map((n) => ({
    id: n.id,
    label: shortLabel(n.label || n.id),
    type: 'rect',
    size: [nodeWidth(n.label || n.id), NODE_H],
    style: {
      fill: COLOR[n.type] || COLOR.default,
      radius: 5,
      stroke: 'transparent',
    },
    labelCfg: {
      position: 'center',
      style: { fontSize: FONT, fill: '#fff' },
    },
  }))

  const edges = rawEdges.map((e, i) => ({
    id: `e${i}`,
    source: e.source,
    target: e.target,
    label: showEdgeLabels ? (e.label || '') : '',
    style: {
      stroke: LEVEL_COLOR[String(e.label || '').match(/Major|Moderate|Minor/)?.[0]]
        || '#c8c9cc',
      lineWidth: 1.2,
      // 只有有方向的关系才画箭头。无向关系画箭头（尤其双箭头）是**凭空断言**。
      endArrow: e.directed === true,
      endArrowType: 'vee',
    },
    labelCfg: showEdgeLabels
      ? {
          autoRotate: true,
          // ⚠️ 把标签**垂直于线**推出 10px。默认在中点上，两点靠近时会直接压住节点。
          refY: -10,
          style: {
            fontSize: 10,
            fill: '#606266',
            background: { fill: 'rgba(255,255,255,.9)', padding: [2, 3] },
          },
        }
      : {},
  }))

  // ── 径向布局：中心枢纽 + 一圈对称 ─────────────────────────────
  //
  // ⚠️ **为什么从 force 换成 radial**（用户第三轮截图）：
  //    `force` 布局是**异步**的——节点会持续移动；而 `fitView` 在布局还没稳定时就
  //    把画布放大了，于是节点被甩出视口，只剩一堆连线指向它们原来的位置。
  //    截图里那种"字号被放大好几倍、线条飞出画面、中间聚成一束无主的线"就是这个症状。
  //    （先确认过不是数据问题：实测两条回答的"边两端不在节点表里"都是 **0 条**。）
  //
  //    `radial` 的位置是**同步算出来**的，配上显式 `fitView()` 就不会错位；
  //    而且"中心枢纽 + 一圈对称"正是用户要的形态。
  const deg = new Map()
  for (const e of rawEdges) {
    deg.set(e.source, (deg.get(e.source) || 0) + 1)
    deg.set(e.target, (deg.get(e.target) || 0) + 1)
  }
  let hub = null
  let best = -1
  for (const n of nodes) {
    const d = deg.get(n.id) || 0
    if (d > best) { best = d; hub = n.id }
  }

  graph = new G6.Graph({
    container: box.value,
    width: box.value.clientWidth || 640,
    height: 340,
    layout: hub && nodes.length > 2
      ? {
          type: 'radial',
          focusNode: hub,
          // 一圈排开要够宽：内圈半径随节点数增长
          unitRadius: nodes.length <= 6 ? 170 : 120,
          preventOverlap: true,
          nodeSize: 64,
          strictRadial: false,
          maxIteration: 300,
          animate: false,
        }
      // 只有 1~2 个节点时径向没有意义，用 grid 保证不重叠
      : { type: 'grid', preventOverlap: true, nodeSize: 80 },
    modes: { default: ['drag-canvas', 'zoom-canvas', 'drag-node'] },
    defaultNode: { type: 'rect' },
    defaultEdge: { type: 'line' },
    animate: false,
  })
  graph.data({ nodes, edges })
  graph.render()
  // ⚠️ **显式再 fit 一次**：依赖构造参数里的 `fitView` 不够可靠——
  //    它可能在布局落位之前就计算了缩放。这一行是"节点被甩出视口"的直接解药。
  try { graph.fitView(30) } catch { /* 空图时 fitView 会抛，忽略 */ }
}

const nothing = computed(() =>
  !props.graph || !(props.graph.nodes || []).length)

onMounted(draw)
watch(() => props.graph, draw, { deep: true })
onBeforeUnmount(() => { if (graph) graph.destroy() })
</script>

<template>
  <div>
    <p v-if="summary" style="color:#606266;font-size:12px;margin:0 0 6px"
       data-test="graph-summary">{{ summary }}</p>
    <div v-show="!nothing" ref="box" data-test="evidence-graph"
         style="border:1px solid #ebeef5;border-radius:4px;background:#fafafa" />
    <p v-if="nothing" style="color:#909399;font-size:12px;margin:0">
      这个回答没有可画的证据子图。
    </p>
    <p style="color:#909399;font-size:12px;margin:6px 0 0">
      按实体类型着色（蓝＝药物，绿＝疾病，橙＝人群）。
      <b>连线不带箭头＝无向关系（如药物相互作用）</b>；带箭头＝有方向（如「禁用于」）。
      <span v-if="note" style="color:#b88230"> ⚠️ {{ note }}</span>
    </p>
  </div>
</template>
