import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// ⚠️ 用**代理**而不是让前端直连后端：跨域（CORS）在开发期是最常见的"页面全白、
//    控制台报错、但后端日志什么都没有"的来源。代理让浏览器只看到同源请求。
//
//    后端路由挂在**根路径**（/auth/login、/medicine-box…），所以这里把 /api 前缀
//    去掉再转发（rewrite）。这样前端只认 /api，将来后端换域名也不用改前端代码。
export default defineConfig({
  plugins: [vue()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ''),
      },
    },
  },
})
