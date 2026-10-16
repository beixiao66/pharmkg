import io
import sys
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
BASE = "http://127.0.0.1:5173"
MODULES = [
    "/src/main.js",
    "/src/api.js",
    "/src/App.vue",
    "/src/views/LoginView.vue",
    "/src/views/MedicineBoxView.vue",
    "/src/components/RiskCard.vue",
    "/src/components/EvidenceGraph.vue",
]

ok = bad = 0
for m in MODULES:
    try:
        with urllib.request.urlopen(BASE + m, timeout=25) as r:
            body = r.read().decode("utf-8", "replace")
        # vite 编译失败的 SFC 会返回 200 + 一段报错，所以不能只看状态码
        broken = ("Internal server error" in body or "Pre-transform error" in body
                  or "SyntaxError" in body or "Failed to parse" in body)
        if r.status == 200 and not broken:
            ok += 1
            print(f"  ✓ {m}   ({len(body)} 字符)")
        else:
            bad += 1
            print(f"  ✗ {m}   HTTP {r.status}  编译报错：{body[:200]}")
    except Exception as exc:  # noqa: BLE001
        bad += 1
        print(f"  ✗ {m}   {type(exc).__name__}: {exc}")

print()
print(f"  模块编译：通过 {ok} / 失败 {bad}")
print()
print("  ⚠️ 这只证明**编译得过**，不证明**页面真的渲染出来**——")
print("     那需要真浏览器（Playwright）。别把这一步当成渲染验收。")
sys.exit(1 if bad else 0)
