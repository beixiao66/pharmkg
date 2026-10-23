/**
 * 关系类型 → 面向用户的中文标签。
 *
 * ⚠️ 为什么要单独一个模块：这两个页面（问答的证据子图、图谱浏览）都要显示边标签，
 * 而图谱里的 `type` 是**关系类型的原始英文名**（`INTERACTS_WITH`）。
 * 用户实测时截图显示边上是 `INTERACTS_WITH` —— 那是给开发者看的，不是给用户看的。
 *
 * 没收录的类型**原样返回**，不硬翻：翻错比不翻更糟。
 */
const TYPE_LABEL = {
  INTERACTS_WITH: '相互作用',
  CONTRAINDICATED_FOR: '禁忌',
  CAUTION_FOR: '慎用',
  RECOMMEND_DRUG: '推荐用药',
  RECOMMEND_FOOD: '推荐食物',
  NOTEAT_FOOD: '忌口',
  HAS_SYMPTOM: '症状',
  ACCOMPANY_DISEASE: '伴随疾病',
  NEED_CHECK: '需要检查',
  BELONGS_TO: '属于',
  SameAs: '别名',
}

export function typeLabel(t) {
  if (!t) return ''
  return TYPE_LABEL[t] || t
}

/** 严重程度 → 中文（与后端 LEVEL_INFO 保持一致，别让两处说法不同）。 */
const LEVEL_LABEL = {
  Major: '高风险',
  Moderate: '中等风险',
  Minor: '轻微',
  Unknown: '严重程度未知',
}

export function levelLabel(lv) {
  if (!lv) return ''
  return LEVEL_LABEL[lv] || lv
}

/**
 * 这个关系类型在语义上**有没有方向**？
 *
 * ⚠️ 为什么必须单独判定，而不是统一画箭头：
 * 图里所有关系都存成 `(a)-[r]->(b)`，但**存储方向 ≠ 语义方向**。
 * `INTERACTS_WITH` 是**无向**的——画单箭头暗示"药 A 作用于药 B"（假的），
 * 画双箭头暗示"存在两个关系"（也是假的）。**无向关系就该是一条线。**
 *
 * 有方向的例子：`禁忌`（药 → 人群/疾病）、`推荐用药`（病 → 药）、`忌口`（病 → 食物）。
 */
const UNDIRECTED = new Set([
  'INTERACTS_WITH',     // 药物相互作用：对称
  'HAS_SYMPTOM',        // 病 - 症状：关联
  'ACCOMPANY_DISEASE',  // 伴随疾病：对称
  'SameAs',             // 别名：对称
])

export function isDirected(t) {
  if (!t) return false
  return !UNDIRECTED.has(t)
}

export default typeLabel
