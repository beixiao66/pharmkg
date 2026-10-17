<script setup>
import { computed } from 'vue'

const props = defineProps({ risk: { type: Object, required: true } })
const TAG = { Major: 'danger', Moderate: 'warning', Minor: 'info' }
const LABEL = { Major: '高风险', Moderate: '中等', Minor: '轻微',
                Unknown: '严重程度未知' }
//: 决策 #42 把「人群」与「过敏史」拆成两个字段，这里合起来只用于判断"设没设过"
const hasAnyTag = computed(() =>
  ((props.risk.population_tags || []).length +
   (props.risk.allergy_history || []).length) > 0)
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
      <!-- ⚠️ 原来这里是 **type='success'（绿色）**，而中文 UI 里绿色＝安全＝可以用。
           那是在用"安全色"暗示一件我们并不知道的事：
           图谱没收录这对组合，可能是「真的没有」，也可能是「我们没收录」——
           在这个系统里后者是压倒性的（全图只有 15 万条 DDInter 边）。
           现在改成**灰色中性**，并把 risk_hint 显式打出来。 -->
      <el-tag v-else type="info" effect="plain" style="margin-left:8px"
              data-test="no-record">未收录</el-tag>
      <div v-if="p.risk_hint" data-test="risk-hint"
           style="color:#b88230;font-size:12px;margin-top:4px">
        ⚠️ {{ p.risk_hint }}
      </div>
      <div style="color:#909399;font-size:12px;margin-top:4px">
        {{ p.source ? '来源：' + p.source : (p.note || '') }}
      </div>
    </el-card>

    <!-- 决策 #42：人群与过敏史是两个字段，这里分开显示，别再说「人群 / 过敏史」 -->
    <h3>② 我的特殊人群与过敏史提示</h3>
    <el-alert v-if="!hasAnyTag" type="info" :closable="false"
              title="还没有设置人群标签或过敏史——设置了才会做这一项检查。" />
    <template v-else>
      <div style="font-size:12px;color:#606266;margin-bottom:6px">
        已设：{{ [...(risk.population_tags || []), ...(risk.allergy_history || [])].join('、') }}
      </div>
      <el-alert v-if="!risk.population.length" type="warning" :closable="false"
                :title="'你的标签在药箱里没有查到禁忌记录。'
                        + '注意：这不等于没有风险 —— 见下面的数据覆盖。'" />
      <el-card v-for="(h, i) in risk.population" :key="i" shadow="never"
               style="margin-bottom:8px" :data-test="`pop-${i}`">
        <b>{{ h.drug }}</b> 禁用于 <el-tag type="danger">{{ h.tag }}</el-tag>
        <span v-if="h.graph_node && h.graph_node !== h.tag"
              style="color:#c0c4cc;font-size:11px;margin-left:6px">
          （图谱节点：{{ h.graph_node }}）
        </span>
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
