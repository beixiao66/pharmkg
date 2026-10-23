<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import G6 from '@antv/g6'

// ⚠️ 这里钉的是 **G6 v4**（package.json 里 ^4.8）。v5 的 API 完全不同。
//
// ## 为什么坐标是**自己算**的，不用 G6 的布局引擎
//
// 这是踩了三轮之后的结论，值得写清楚：
//
// * 第一版：不指定 layout → G6 把节点摆成**一条直线**（137 个节点挤成一横排）。
// * 第二版：改用 `force` → 布局是**异步**的，节点会持续移动，而 `fitView`
//   在落位前就把画布放大了 → **节点被甩出视口**，只剩一堆连线指向原位置。
// * 第三版：改用 `radial` + 渲染后 `fitView(30)` → `fitView` 抛错被我 try/catch 吞掉
//   → 画布停在默认视角、节点在视口外 → **整张图空白**（而 DOM 层的摘要照常显示）。
//
// 三次都是"布局引擎行为不确定"导致的。而我们要的形态非常简单：
// **枢纽在正中间、其余等角距排在外圈**——那就是几行三角函数。
// **自己算坐标 = 同步、确定、不需要 fitView 兜底。**
//
// ## 其余几条不变的设计
//
// * **无向关系不画箭头**（`INTERACTS_WITH` 无向；单箭头/双箭头都是无根据的暗示）；
// * **边多时不画边标签**（>6 条），关系类型由下方文字摘要承担；
// * **节点上限 15**，超出按度数取前 15，并在摘要里说清"另有 N 个未画出"；
// * 节点是**圆角矩形、宽度按字数算**，超过 12 字截断成 `…`。
const props = defineProps({
  graph: { type: Object, required: true },
  maxNodes: { type: Number, default: 15 },
})

const box = ref(null)
const note = ref('')
const summary = ref('')
const canvasH = ref(340)
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
const MAX_LABELED_EDGES = 6
/** 相邻节点之间留的横向空隙（算环半径用） */
const GAP = 18
/** 每环之间的径向间距 */
const RING_GAP = 74

function shortLabel(s) {
  if (!s) return ''
  return s.length > MAX_LABEL ? s.slice(0, MAX_LABEL) + '…' : s
}
function nodeWidth(label) {
  return Math.min(180, Math.max(56, shortLabel(label).length * CHAR_W + 18))
}

function pick(src) {
  const all = (src.nodes || []).filter((n) => n && n.id)
  const deg = new Map(all.map((n) => [n.id, 0]))
  for (const e of src.edges || []) {
    if (deg.has(e.source)) deg.set(e.source, deg.get(e.source) + 1)
    if (deg.has(e.target)) deg.set(e.target, deg.get(e.target) + 1)
  }
  const kept = all.length <= props.maxNodes
    ? all
    : [...all].sort((a, b) => (deg.get(b.id) || 0) - (deg.get(a.id) || 0)).slice(0, props.maxNodes)
  const ids = new Set(kept.map((n) => n.id))
  const edges = (src.edges || []).filter((e) => ids.has(e.source) && ids.has(e.target))
  return { nodes: kept, edges, total: all.length, truncated: kept.length < all.length, deg }
}

/**
 * **自己算坐标**：枢纽居中，其余等角距向外环排。
 *
 * 环的容量按"该环周长能放下几个节点"算，放不下就开下一环——
 * 这样节点数变化时**永远不会有重叠**，也不依赖任何布局引擎。
 */
function layout(nodes, hubId, widths, W) {
  const pos = new Map()
  const cx = W / 2
  const rest = nodes.filter((n) => n.id !== hubId)

  let ring = 1
  let idx = 0
  const rings = []
  while (idx < rest.length) {
    const R = 60 + ring * RING_GAP
    const avg = rest.slice(idx).reduce((s, n) => s + (widths.get(n.id) || 90), 0)
      / Math.max(1, rest.length - idx)
    const cap = Math.max(3, Math.floor((2 * Math.PI * R) / (avg + GAP)))
    const cnt = Math.min(cap, rest.length - idx)
    rings.push({ R, from: idx, cnt, ring })
    idx += cnt
    ring += 1
  }
  const maxR = rings.length ? rings[rings.length - 1].R : 0
  const H = Math.max(320, 2 * (maxR + NODE_H + 24))
  const cy = H / 2
  if (hubId) pos.set(hubId, { x: cx, y: cy })
  for (const rg of rings) {
    for (let k = 0; k < rg.cnt; k++) {
      // ⚠️ **每一环必须错开起始角**，否则内环与外环在同一个角度的两个节点
      //    会与圆心**完全共线**——「中心→外环节点」的连线会从内环节点背后穿过去，
      //    看起来就像"内环节点和外环节点之间有一条带箭头的边"。
      //    实测就是这么误判出来的：骨肽片（内环 0°）与骨刺片（外环 0°）共线，
      //    用户看到 骨肽片→骨刺片 的箭头，而数据里根本没有这条边
      //    （查证：23 条边全是 类风湿关节炎→某药，两端都不含枢纽的有 0 条）。
      //    （第一版这里写了注释但表达式恒为 0，等于没做。）
      const a = (2 * Math.PI * k) / rg.cnt + (rg.ring - 1) * (Math.PI / rg.cnt)
      pos.set(rest[rg.from + k].id, {
        x: cx + rg.R * Math.cos(a),
        y: cy + rg.R * Math.sin(a),
      })
    }
  }
  return { pos, H }
}

async function draw() {
  // ⚠️ **必须等 DOM 更新完**。watch 回调在 DOM patch **之前**执行，
  //    此时容器可能还没进 DOM（`v-if` 刚翻成 true）或宽度还是 0，
  //    G6 在零宽/隐藏容器上渲染 = **一片空白**。
  //    "两节点那张图空着"就是这一类时序问题。
  await nextTick()
  if (graph) { graph.destroy(); graph = null }
  const el = box.value
  if (!el) return

  const { nodes: rawNodes, edges: rawEdges, total, truncated, deg } = pick(props.graph || {})
  if (!rawNodes.length) {
    summary.value = ''
    note.value = ''
    graph = null
    return
  }

  const widths = new Map(rawNodes.map((n) => [n.id, nodeWidth(n.label || n.id)]))
  // 枢纽 = 度数最高的节点（骨肽片 137 / 类风湿关节炎 23），天然是中心
  let hubId = null
  let best = -1
  for (const n of rawNodes) {
    const d = deg.get(n.id) || 0
    if (d > best) { best = d; hubId = n.id }
  }

  // ⚠️ 取不到宽度时兜底 640 并在控制台喊一声（而不是静默画在 0 宽度里）。
  //    上一版用 `v-show` 隐藏容器，隐藏时 `clientWidth` 是 0，
  //    G6 在隐藏/零宽容器上渲染的结果就是**一片空白**——两节点那张图就是这么空的。
  const measured = el.clientWidth || 0
  const W = measured > 40 ? measured : 640
  if (measured <= 40) {
    console.warn('[EvidenceGraph] 容器宽度取到', measured, '，回退到 640 渲染')
  }
  const { pos, H } = layout(rawNodes, hubId, widths, W)
  canvasH.value = H

  // 摘要 + 截断说明（图形放弃的细节由文字承担）
  const byType = {}
  for (const n of rawNodes) {
    const t = TYPE_CN[n.type] || n.type || '实体'
    byType[t] = (byType[t] || 0) + 1
  }
  const typePart = Object.entries(byType).map(([k, v]) => `${v} 个${k}`).join('、')
  const kinds = [...new Set(rawEdges.map((e) => e.label).filter(Boolean))]
  const relPart = kinds.length
    ? `关系：${kinds.slice(0, 3).join('、')}${kinds.length > 3 ? ` 等 ${kinds.length} 种` : ''}`
    : '无连线'
  const cut = truncated ? `；另有 ${total - rawNodes.length} 个未画出，完整清单见上面的答案` : ''
  summary.value = `图中画了 ${rawNodes.length} 个节点（${typePart}），${rawEdges.length} 条连线（${relPart}）${cut}。`
  note.value = truncated ? `节点过多，只画了关联最多的 ${rawNodes.length} 个（共 ${total} 个）。` : ''

  const showEdgeLabels = rawEdges.length <= MAX_LABELED_EDGES

  const nodes = rawNodes.map((n) => {
    const p = pos.get(n.id) || { x: W / 2, y: H / 2 }
    return {
      id: n.id,
      label: shortLabel(n.label || n.id),
      type: 'rect',
      x: p.x,
      y: p.y,
      size: [widths.get(n.id), NODE_H],
      style: {
        fill: COLOR[n.type] || COLOR.default,
        radius: 5,
        stroke: n.id === hubId ? '#303133' : 'transparent',
        lineWidth: n.id === hubId ? 2 : 0,
      },
      labelCfg: { position: 'center', style: { fontSize: FONT, fill: '#fff' } },
    }
  })

  const edges = rawEdges.map((e, i) => ({
    id: `e${i}`,
    source: e.source,
    target: e.target,
    label: showEdgeLabels ? (e.label || '') : '',
    style: {
      stroke: LEVEL_COLOR[String(e.label || '').match(/Major|Moderate|Minor/)?.[0]] || '#c8c9cc',
      lineWidth: 1.2,
      endArrow: e.directed === true,
      endArrowType: 'vee',
    },
    labelCfg: showEdgeLabels
      ? {
          autoRotate: true,
          refY: -10,
          style: {
            fontSize: 10, fill: '#606266',
            background: { fill: 'rgba(255,255,255,.9)', padding: [2, 3] },
          },
        }
      : {},
  }))

  // ⚠️ **不给 layout**：节点自带 x/y，G6 直接用。
  //    也不设 fitView —— 坐标是按当前画布尺寸算的，本来就都落在可视区内。
  graph = new G6.Graph({
    container: el,
    width: W,
    height: H,
    modes: { default: ['drag-canvas', 'zoom-canvas', 'drag-node'] },
    defaultNode: { type: 'rect' },
    defaultEdge: { type: 'line' },
    animate: false,
  })
  graph.data({ nodes, edges })
  graph.render()
}

const nothing = computed(() => !props.graph || !(props.graph.nodes || []).length)

onMounted(draw)
watch(() => props.graph, draw, { deep: true })
onBeforeUnmount(() => { if (graph) graph.destroy() })
</script>

<template>
  <div>
    <p v-if="summary" style="color:#606266;font-size:12px;margin:0 0 6px"
       data-test="graph-summary">{{ summary }}</p>
    <div v-if="!nothing" ref="box" data-test="evidence-graph"
         :style="{ border: '1px solid #ebeef5', borderRadius: '4px',
                   background: '#fafafa', height: canvasH + 'px' }" />
    <p v-if="nothing" style="color:#909399;font-size:12px;margin:0">
      这个回答没有可画的证据子图。
    </p>
    <p v-else style="color:#909399;font-size:12px;margin:6px 0 0">
      按实体类型着色（蓝＝药物，绿＝疾病，橙＝人群）；<b>中心带深色描边的是枢纽</b>。
      <span v-if="note" style="color:#b88230"> ⚠️ {{ note }}</span>
    </p>
  </div>
</template>
