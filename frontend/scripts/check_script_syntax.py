"""静态校验 SFC 的 <script setup> 语法（用 node --check）。

为什么这么做：起 vite 要提权、还会占端口，而我只需要知道「改动有没有把语法写坏」。
把 script 块抽成 .mjs 交给 node --check，足够抓出括号/引号这类错误。
注意：**这不等价于完整编译**（模板部分没查），只是最小限度的防线。
"""

import io
import pathlib
import re
import subprocess
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
NODE = (r"C:\Users\beixiao\.dsh\dsh-runtimes\dsh-primary-runtime"
        r"\dependencies\node\bin\node.exe")
TMP = pathlib.Path("_tmp/syntaxcheck")
TMP.mkdir(parents=True, exist_ok=True)

files = sorted(pathlib.Path("frontend/src").rglob("*.vue")) + \
    sorted(pathlib.Path("frontend/src").rglob("*.js"))
bad = 0
for f in files:
    src = f.read_text(encoding="utf-8")
    if f.suffix == ".vue":
        m = re.search(r"<script setup>(.*?)</script>", src, re.S)
        if not m:
            print(f"  – {f.name}  没有 <script setup>，跳过")
            continue
        code = m.group(1)
    else:
        code = src
    tmp = TMP / (f.stem + ".mjs")
    tmp.write_text(code, encoding="utf-8", newline="\n")
    r = subprocess.run([NODE, "--check", str(tmp)], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if r.returncode == 0:
        print(f"  ✓ {f.relative_to('frontend')}  script 语法通过")
    else:
        bad += 1
        print(f"  ✗ {f.relative_to('frontend')}")
        print("     " + (r.stderr or "").strip().split("\n")[0:4].__str__())

print()
print(f"  script 语法：失败 {bad} 个")
print("  ⚠️ 这只查 script 块，**模板部分没查**——模板错误要靠浏览器/HMR 报出来。")
sys.exit(1 if bad else 0)
