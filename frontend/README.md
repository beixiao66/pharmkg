# pharmkg 前端（L6 药箱页）

用 **Vite + Vue3 + Element Plus + AntV G6**。第一版只做**药箱页**
（用户拍板：一页做透，问答页与数据看板后续单独做）。

## 跑起来

```powershell
$node = "C:\Users\beixiao\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe"
$pnpm = "C:\Users\beixiao\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\pnpm\bin\pnpm.mjs"

cd frontend
& $node $pnpm install
& $node $pnpm dev          # → http://127.0.0.1:5173
```

**后端要同时开着**（前端通过 Vite 代理把 `/api` 转到 `127.0.0.1:8000`）：

```powershell
cd backend
.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

## 两个刻意的选择

1. **用代理而不是直连后端**：跨域在开发期是最常见的"页面全白、控制台报错、
   后端日志却什么都没有"的来源。代理让浏览器只看到同源请求。
2. **G6 钉 v4**（`^4.8`）：v5 的 API 完全不同（`new Graph({data, node, edge})` 那一套），
   混用会直接报错。要升 v5 得整体重写这个组件。

## 页面上的四个数据-test 锚点

Playwright 就是靠它们定位的（`data-test="drug-q"` 等），改样式不影响验收脚本。
