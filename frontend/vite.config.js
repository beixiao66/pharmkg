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
  build: {
    commonjsOptions: {
      // ⚠️ **这一行是容器版白屏的解药，删了会复发。**
      //
      // 症状：`pnpm dev` 一切正常，但 `vite build` 出来的包在浏览器里报
      //      `Uncaught ReferenceError: require is not defined` → Vue 挂不上 → 白屏。
      //
      // 根因：`@antv/g6` v4 及其底层（`@antv/g-base` 等）是**混合格式**的——
      //      文件看起来是 ESM，里面却写着 `require("../package.json")` 来读自身版本号。
      //      Rollup 的 commonjs 插件**默认只处理纯 CJS 文件**，于是这句 `require` 原样
      //      漏进产物。实测产物里共有 5 处 `require(`，其中两处正是
      //      `require("../package.json")`（另几处是 UMD 的 `x.require && …` 保护调用，
      //      有 try/catch 不会抛）。
      //
      // 为什么 dev 不炸：开发服务器用 esbuild **预打包**依赖，CJS 被自动兼容了。
      //      **"dev 能跑"因此完全不能证明"build 能跑"**——这类问题只在生产构建暴露。
      transformMixedEsModules: true,
    },
  },
})
