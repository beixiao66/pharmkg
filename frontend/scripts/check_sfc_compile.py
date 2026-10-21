import io
import pathlib
import sys
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
#: 端口可用命令行覆盖（默认 5173；我在沙箱里验证时用 5174，避开用户的端口）。
#: 之前靠 `exec(...).replace('5173','5174')` 这种方式覆盖，结果 `__file__` 不见了——
#: **用参数比替换源码可读也可靠**。
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 5173
BASE = f"http://127.0.0.1:{PORT}"

#: ⚠️ **自动发现**而不是写死清单。
#: 写死清单**会漏掉新加的文件**——这次加 `AskView.vue` 时就漏了：
#: 检查打印"7/7 通过"，而新页面根本没被编译过。**一个会漏掉新文件的检查，
#: 比没有检查更危险**（它给的是假的安全感）。
_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
MODULES = sorted(
    "/src/" + str(p.relative_to(_SRC)).replace("\\", "/")
    for p in list(_SRC.rglob("*.vue")) + list(_SRC.rglob("*.js"))
)

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
