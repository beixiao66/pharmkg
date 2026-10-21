# pharmkg 前端镜像（构建 → nginx 静态托管 + /api 反向代理）
#
# ## 为什么前端要自己带反代
#
# 开发期靠 Vite 的 proxy 把 `/api` 转到后端（`frontend/vite.config.js`）；
# 生产环境没有 Vite，**必须由 nginx 承担同一个职责**：
# 浏览器只看到同源请求，后端地址不暴露给页面，也不用配 CORS。
#
# 若不做这一步，页面会全白、控制台报 CORS，而后端日志什么都没有——
# 这是前后端分离部署最常见、也最费时间的一类问题。

# ── 构建阶段 ──────────────────────────────────────────────
FROM node:22-slim AS build

WORKDIR /app

# 先拷锁文件，让依赖层可缓存
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
# ⚠️ 两个来自实测的坑：
#
# 1. **`pnpm-workspace.yaml` 里的 `allowBuilds: {esbuild: true}` 必须一起拷进来**。
#    pnpm 11 靠它决定要不要执行 esbuild 的构建脚本；缺了它，`vite build` 会报
#    `Cannot find package 'esbuild'`——**而这个报错完全不提 pnpm 的构建脚本策略**，
#    极难定位（开发期就为这个折腾过一轮）。
#
# 2. **抗慢网络**：实测本机容器的 npm registry **单个 ping 就要 30 秒**
#    （`npm ping` → PONG 30249ms）。默认的并发下载 + 短超时会让 `pnpm install`
#    直接失败，而报错只有一句 exit 1。所以这里显式降并发、拉长超时——
#    慢可以等，失败不行。
RUN corepack enable \
    && corepack prepare pnpm@11.7.0 --activate \
    && pnpm install --frozen-lockfile \
       --network-concurrency=1 \
       --fetch-timeout=300000 \
       --config.confirmModulesPurge=false

COPY frontend/ ./
RUN pnpm build

# ── 运行阶段 ──────────────────────────────────────────────
FROM nginx:1.27-alpine

COPY --from=build /app/dist /usr/share/nginx/html
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
