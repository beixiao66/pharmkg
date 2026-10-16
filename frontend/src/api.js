import axios from 'axios'

// 走 Vite 的 /api 代理（见 vite.config.js），所以这里不写后端地址
const api = axios.create({ baseURL: '/api', timeout: 20000 })

// 每个请求自动带上 JWT；没有令牌就不带（登录/注册本来也不需要）
api.interceptors.request.use((cfg) => {
  const t = localStorage.getItem('token')
  if (t) cfg.headers.Authorization = `Bearer ${t}`
  return cfg
})

// 401 统一处理：**清掉令牌并回到登录页**。
// 不清的话页面会一直停在"接口全 401"的状态，看起来像坏了。
api.interceptors.response.use(
  (r) => r,
  (err) => {
    if (err.response && err.response.status === 401) {
      localStorage.removeItem('token')
      window.dispatchEvent(new Event('pharmkg:logout'))
    }
    return Promise.reject(err)
  },
)

// ---------------------------------------------------------------- 错误信息「说人话」

//: 字段名 → 中文。**没收录的就原样显示**，不硬翻（硬翻更容易翻错）。
const FIELD_CN = {
  username: '用户名', password: '密码', tags: '人群标签',
  drug_id: '药物', content: '内容', title: '标题',
  rewritten_query: '改写后的问题', cypher: '查询语句',
  evidence: '证据', role: '角色',
}

//: pydantic 的错误类型 → 中文说法。占位符从 ctx 里取。
const TYPE_CN = {
  string_too_short: (f, c) => `${f}太短了，至少要 ${c?.min_length ?? '?'} 个字符`,
  string_too_long: (f, c) => `${f}太长了，最多 ${c?.max_length ?? '?'} 个字符`,
  missing: (f) => `请填写${f}`,
  int_parsing: (f) => `${f}必须是整数`,
  greater_than_equal: (f, c) => `${f}不能小于 ${c?.ge ?? '?'}`,
  less_than_equal: (f, c) => `${f}不能大于 ${c?.le ?? '?'}`,
  too_short: (f, c) => `${f}至少要有 ${c?.min_length ?? '?'} 项`,
  value_error: (f) => `${f}格式不对`,
}

/**
 * 把后端/网络的错误转成**用户能看懂的一句话**。
 *
 * ⚠️ **为什么必须专门做这件事**：FastAPI 的 422 校验错误里 `detail` 是一个
 *    **数组**（每个出错字段一条），而 409「用户名已存在」那种 `detail` 才是一句话。
 *    第一版只判断了"是不是字符串"，不是字符串就 `JSON.stringify` ——
 *    于是整坨原始 JSON 直接弹给了用户：
 *      `[{"type":"string_too_short","loc":["body","password"],...}]`
 *    **那个兜底对开发方便、对用户是灾难**；兜底必须换成人话。
 */
export function humanizeError(err) {
  // ① 根本没拿到响应 → 多半是后端没开或代理没通。
  //    这条必须说清"去开后端"，否则用户只看到 Network Error、不知道下一步干什么。
  if (!err?.response) {
    return '连不上后端服务。请确认后端已在 8000 端口运行（见 frontend/README.md）。'
  }

  const { status, data } = err.response
  const detail = data?.detail

  // ② detail 是字符串 → 后端已经给了人话（如「用户名或密码不正确」），原样用
  if (typeof detail === 'string' && detail.trim()) return detail

  // ③ detail 是数组 → pydantic 的字段级校验错误，逐条翻成人话
  if (Array.isArray(detail) && detail.length) {
    return detail.slice(0, 3).map((d) => {
      // loc 形如 ['body', 'password']；去掉 body，取最后一个字段名
      const loc = Array.isArray(d?.loc) ? d.loc.filter((x) => x !== 'body') : []
      const raw = String(loc[loc.length - 1] ?? '')
      const field = FIELD_CN[raw] || raw || '输入'
      const fn = TYPE_CN[d?.type]
      if (fn) return fn(field, d?.ctx)
      // 没收录的类型：带上字段名 + 后端原话。**宁可露出英文，也不要露出 JSON。**
      return `${field}：${d?.msg || '格式不对'}`
    }).join('；')
  }

  // ④ 其它状态码 → 按 HTTP 语义说人话
  if (status === 401) return '登录已过期，请重新登录。'
  if (status === 403) return '没有权限做这个操作。'
  if (status === 404) return '没有找到要操作的对象。'
  if (status === 409) return '这条记录已经存在了。'
  if (status >= 500) return `服务端出错了（HTTP ${status}），请稍后再试。`
  return `请求失败（HTTP ${status}）。`
}

export default api
