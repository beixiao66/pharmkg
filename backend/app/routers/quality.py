"""图谱质量的只读接口（供数据看板页用）。

## 为什么直接读产物文件，而不在这里重算

质量问题（自环 112、剂型分裂 1,243…）是**一次图扫描的结论**，重算要几十秒，
而且**必须与报告里的数字完全一致**——看板显示的数与 `docs/图谱质量报告.md`
里的数一旦不同，两份材料就都不可信了。

所以这里只做一件事：**把 `data/quality/graph_quality.json` 原样端出去**。
唯一的真相仍由 `scripts/check_graph_quality.py` 产出。

## 一个必须有的保护：产物可能是旧的

`graph_quality.json` 是**快照**。图上又导入了数据之后它不会自动更新，
而看板如果照旧显示，就是**用旧数字描述新图**——比不显示更糟。
所以响应里带 `generated_at`（文件修改时间），前端把它显示出来，
让看的人自己判断新不新。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status

from app.deps import get_current_user
from app.models import AppUser

router = APIRouter(prefix="/quality", tags=["图谱质量"])

#: backend/app/routers/quality.py → … → 仓库根
REPO_ROOT = Path(__file__).resolve().parents[3]
QUALITY_FILE = REPO_ROOT / "data" / "quality" / "graph_quality.json"


@router.get("/graph", summary="图谱质量快照（L8 诊断产物）")
def graph_quality(user: AppUser = Depends(get_current_user)) -> dict:
    """返回 L8 诊断的原始 JSON，并补上"这份快照有多新"。"""
    if not QUALITY_FILE.exists():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=("还没有质量快照。先跑 "
                    "`python scripts/check_graph_quality.py` 生成 "
                    "data/quality/graph_quality.json。"))
    data = json.loads(QUALITY_FILE.read_text(encoding="utf-8"))
    st = QUALITY_FILE.stat()
    data["_meta"] = {
        "file": str(QUALITY_FILE.relative_to(REPO_ROOT)).replace("\\", "/"),
        "generated_at": datetime.fromtimestamp(
            st.st_mtime, tz=timezone.utc).isoformat(timespec="seconds"),
        "size_bytes": st.st_size,
        # ⚠️ 明确说明它是快照，不是实时统计。
        #    注意：这里是**给界面直接显示的纯文本**，不是 markdown——
        #    用反引号包脚本名会**原样显示**出来（看板截图里就露出来了），
        #    改用中文书名号。
        "note": ("这是「scripts/check_graph_quality.py」跑出来的一次快照；"
                 "图谱重新导入后要重跑该脚本，否则看板显示的是旧图的质量。"),
    }
    return data
