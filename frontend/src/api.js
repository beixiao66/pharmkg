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

export default api
