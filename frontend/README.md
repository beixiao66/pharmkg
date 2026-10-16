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

## 页面上的 data-test 锚点

Playwright 靠它们定位（`username` / `password` / `login` / `register` / `drug-q` /
`drug-search` / `search-hits` / `save-tags` / `tag-select` / `evidence-graph` 等），
改样式不影响验收脚本。

## ⚠️ 怎么判断"前端到底起没起"——别只用一种探测法

**踩过**：我用 `Invoke-WebRequest http://127.0.0.1:5173/` 探测，超时 → 就断言
「5173 没有服务」。**而用户的前端其实一直在跑。** 三种方法一对比，真相立刻出来：

| 探测法 | 对用户手动启的服务（沙箱外） | 对我自己启的服务（沙箱内） |
|---|---|---|
| `netstat -ano \| findstr :5173` | ✓ 看到监听 | ✓ |
| 裸 TCP（`System.Net.Sockets.TcpClient`） | ✓ 连得上 | ✓ |
| `Invoke-WebRequest` | **✗ 超时** | ✓ |

**代理已排除**：`HTTP_PROXY` 等环境变量一个都没设，.NET 默认代理也是空的。
所以不是代理问题，而是**我的 HTTP 探测到不了「沙箱外进程」提供的服务**
（TCP 握手过得去、HTTP 往返过不去）。

两条规矩从此立下：

1. **判断服务在不在，先用 `netstat` + 裸 TCP**——这两样不会骗人；HTTP 探测只作参考。
2. **两种探测法结论不一致时，就说"不确定"**，别挑一个自己喜欢的当结论。
   当时我只跑了一种探测，还把它当事实说了出去。

## 错误提示

所有接口错误都走 `src/api.js` 的 `humanizeError()`。**不要把 `detail` 直接甩给用户**：
FastAPI 的 422 里 `detail` 是**数组**（每个出错字段一条），
`JSON.stringify` 出来是一坨机器话（用户实测撞到过）。

