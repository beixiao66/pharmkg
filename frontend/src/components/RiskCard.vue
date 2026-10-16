<script setup>
defineProps({ risk: { type: Object, required: true } })
const TAG = { Major: 'danger', Moderate: 'warning', Minor: 'info' }
const LABEL = { Major: '高风险', Moderate: '中等', Minor: '轻微',
                Unknown: '严重程度未知' }
</script>

<template>
  <div>
    <h3>① 药物两两相互作用（共 {{ risk.pairs.length }} 对）</h3>
    <el-empty v-if="!risk.pairs.length" description="药箱里不足两味药" />
    <el-card v-for="(p, i) in risk.pairs" :key="i" shadow="never"
             style="margin-bottom:8px" :data-test="`pair-${i}`">
      <b>{{ p.drug_a }}</b> ↔ <b>{{ p.drug_b }}</b>
      <el-tag v-if="p.level" :type="TAG[p.level] || 'info'" style="margin-left:8px">
        {{ LABEL[p.level] || p.level }}（{{ p.level }}）
      </el-tag>
      <el-tag v-else type="success" style="margin-left:8px">图谱中无记录</el-tag>
      <div style="color:#909399;font-size:12px;margin-top:4px">
        {{ p.source ? '来源：' + p.source : (p.note || '') }}
      </div>
    </el-card>

    <h3>② 我的{{ risk.population_tags.length ? '人群 / 过敏史' : '人群' }}提示</h3>
    <el-alert v-if="!risk.population_tags.length" type="info" :closable="false"
              title="还没有设置人群标签——设置了才会做这一项检查。" />
    <template v-else>
      <el-alert v-if="!risk.population.length" type="warning" :closable="false"
                :title="`你的标签（${risk.population_tags.join('、')}）在药箱里没有查到禁忌记录。`
                        + '注意：这不等于没有风险，见下面的数据覆盖。'" />
      <el-card v-for="(h, i) in risk.population" :key="i" shadow="never"
               style="margin-bottom:8px" :data-test="`pop-${i}`">
        <b>{{ h.drug }}</b> 禁用于 <el-tag type="danger">{{ h.tag }}</el-tag>
        <div style="color:#909399;font-size:12px;margin-top:4px">
          说明书原句：{{ h.evidence || '（无）' }}
        </div>
      </el-card>
    </template>

    <h3>③ 数据覆盖（<span style="color:#c0392b">请一定看这一节</span>）</h3>
    <el-descriptions :column="2" border size="small">
      <el-descriptions-item label="药箱药数">{{ risk.coverage.box_size }}</el-descriptions-item>
      <el-descriptions-item label="检查的药对">{{ risk.coverage.pairs_checked }}</el-descriptions-item>
      <el-descriptions-item label="相互作用边总数">
        {{ risk.coverage.interaction_edges_total.toLocaleString() }}
      </el-descriptions-item>
      <el-descriptions-item label="人群禁忌边总数">
        {{ risk.coverage.population_edges_total }}（high {{ risk.coverage.population_edges_high }}）
      </el-descriptions-item>
    </el-descriptions>
    <el-alert type="warning" :closable="false" style="margin-top:8px"
              :title="risk.coverage.note" />
  </div>
</template>
