<script setup>
import { onMounted, onBeforeUnmount, ref, watch } from 'vue'
import G6 from '@antv/g6'

// ⚠️ 这里钉的是 **G6 v4**（package.json 里 ^4.8）。v5 的 API 完全不同
//    （`new Graph({data, node, edge})` 那一套），不能混用。
//
// ## 这一版重写了三件事（都有用户实测截图作为起因）
//
// 1. **节点从圆改成圆角矩形，宽度按字数算**：中文药名 5–12 字，塞进 44px 的圆里
//    必然溢出，截图里标签叠成一团糊。现在 `width = 字数*13 + 18`，超长截断成 `…`。
//
// 2. **无向关系不画箭头**：`INTERACTS_WITH` 在图里是**无向**的，存成
//    `A -[r]-> B` 只是 Neo4j 的存储方向。**单箭头暗示方向，不对；
//    双箭头暗示"两个关系"，同样不对**——正确做法是**不画箭头**。
//    只有真正有方向的关系（禁忌 = 药→人群/疾病、推荐用药 = 病→药）才带箭头，
//    由边上的 `directed: true` 决定。
//
// 3. **节点太多就不画全**：截图里 137 个节点的星形图挤成一条横线，毫无可读性。
//    现在按**度数**取前 N 个（保留枢纽），并在图下方**明说"只画了 M/N 个"**——
//    与项目里"截断必须显式"的原则一致。
const props = defineProps({
  graph: { type: Object, required: true },
  /** 最多画多少个节点。超过就按度数取前 N 个并在图下注明。 */
  maxNodes: { type: Number, default: 30 },
})

const box = ref(null)
const note = ref('')
let graph = null

const COLOR = {
  Drug: '#409eff',
  Disease: '#67c23a',
  Population: '#f0a020',
  Symptom: '#909399',
  default: '#b1b3b8',
}
//: 边的颜色：标签里带 level 就按严重程度上色，否则中性灰
const LEVEL_COLOR = { Major: '#c0392b', Moderate: '#e67e22', Minor: '#95a5a6' }

const FONT = 11
const CHAR_W = 13
const NODE_H = 28
const MAX_LABEL = 12

function shortLabel(s) {
  if (!s) return ''
  return s.length > MAX_LABEL ? s.slice(0, MAX_LABEL) + '…' : s
}

function nodeWidth(label) {
  return Math.min(180, Math.max(56, shortLabel(label).length * CHAR_W + 18))
}

/** 按度数取前 maxNodes 个节点；返回裁剪后的图与"是否截断"。 */
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

function draw() {
  if (!box.value) return
  if (graph) { graph.destroy(); graph = null }

  const { nodes: rawNodes, edges: rawEdges, total, truncated } = pick(props.graph || {})
  note.value = truncated
    ? `节点太多，这里只画了关联最多的 ${rawNodes.length} 个（共 ${total} 个）——完整清单见上面的答案。`
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
      // 标签默认就在图形中心；显式写出来，避免以后改默认值时跑偏
      position: 'center',
      style: { fontSize: FONT, fill: '#fff' },
    },
  }))

  const edges = rawEdges.map((e, i) => ({
    id: `e${i}`,
    source: e.source,
    target: e.target,
    label: e.label || '',
    style: {
      stroke: LEVEL_COLOR[String(e.label || '').match(/Major|Moderate|Minor/)?.[0]]
        || '#c8c9cc',
      lineWidth: 1.2,
      // ⚠️ 只有有方向的关系才画箭头。无向关系画箭头（尤其双箭头）是**凭空断言**。
      endArrow: e.directed === true,
      endArrowType: 'vee',
    },
    labelCfg: {
      autoRotate: true,
      style: {
        fontSize: 10,
        fill: '#606266',
        background: { fill: '#fff', padding: [2, 3] },
      },
    },
  }))

  graph = new G6.Graph({
    container: box.value,
    width: box.value.clientWidth || 640,
    height: 320,
    // ⚠️ 不给 layout 时 G6 会把节点摆成一条线（截图里就是这样），必须显式指定。
    layout: {
      type: 'force',
      preventOverlap: true,
      nodeSize: 46,
      linkDistance: 130,
      nodeStrength: -60,
      edgeStrength: 0.4,
      animate: false,
    },
    modes: { default: ['drag-canvas', 'zoom-canvas', 'drag-node'] },
    defaultNode: { type: 'rect' },
    defaultEdge: { type: 'line' },
    fitView: true,
    fitViewPadding: [16, 16, 16, 16],
    animate: false,
  })
  graph.data({ nodes, edges })
  graph.render()
}

onMounted(draw)
watch(() => props.graph, draw, { deep: true })
onBeforeUnmount(() => { if (graph) graph.destroy() })
</script>

<template>
  <div>
    <div ref="box" data-test="evidence-graph"
         style="border:1px solid #ebeef5;border-radius:4px;background:#fafafa" />
    <p style="color:#909399;font-size:12px;margin:6px 0 0">
      按实体类型着色（蓝＝药物，绿＝疾病，橙＝人群）。
      <b>连线不带箭头＝无向关系（如药物相互作用）</b>；带箭头＝有方向（如「禁用于」）。
    </p>
    <p v-if="note" style="color:#b88230;font-size:12px;margin:2px 0 0"
       data-test="graph-truncated">⚠️ {{ note }}</p>
  </div>
</template>
