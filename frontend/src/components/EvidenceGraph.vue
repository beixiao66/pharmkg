<script setup>
import { onMounted, onBeforeUnmount, ref, watch } from 'vue'
import G6 from '@antv/g6'

// ⚠️ 这里钉的是 **G6 v4**（package.json 里 ^4.8）。v5 的 API 完全不同
//    （`new Graph({data, node, edge})` 那一套），不能混用。
const props = defineProps({ graph: { type: Object, required: true } })
const box = ref(null)
let graph = null

const LEVEL_COLOR = { Major: '#c0392b', Moderate: '#e67e22', Minor: '#95a5a6' }

function draw() {
  if (!box.value) return
  if (graph) { graph.destroy(); graph = null }
  const nodes = props.graph.nodes.map((n) => ({
    id: n.id,
    label: n.label,
    size: n.type === 'Drug' ? 44 : 34,
    style: { fill: n.type === 'Drug' ? '#409eff' : '#f0a020' },
  }))
  const edges = props.graph.edges.map((e, i) => ({
    id: `e${i}`,
    source: e.source,
    target: e.target,
    label: e.label || '',
    style: { stroke: LEVEL_COLOR[e.label] || '#bbb', endArrow: true },
  }))
  graph = new G6.Graph({
    container: box.value,
    width: box.value.clientWidth || 640,
    height: 300,
    fitView: true,
    modes: { default: ['drag-canvas', 'zoom-canvas'] },
    defaultNode: { type: 'circle', labelCfg: { style: { fontSize: 11 } } },
    defaultEdge: { labelCfg: { style: { fontSize: 10, fill: '#666' } } },
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
      蓝＝药物，橙＝禁忌对象（人群）。边上的字是严重程度或关系名。
    </p>
  </div>
</template>
