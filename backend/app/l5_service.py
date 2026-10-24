"""L5 问答的服务封装（把脚本里的六步流水线接进 FastAPI）。

## 为什么要这一层，而不是把 `scripts/l5_*.py` 抄进 backend

那六个脚本（意图 / 实体链接 / 改写 / text2cypher / 答案 / 流水线，约 1,900 行）
**已经在做正确的事**，而且是被 `l5_pipeline.py` 与自测验证过的。
抄一份进 backend 会立刻产生两个实现，然后慢慢漂移——**这是重复实现最贵的形状**。

所以这里只做三件事：

1. **把 `scripts/` 挂进 `sys.path`**，让 backend 能 `import` 那些模块；
2. **把代价高的东西做成进程级单例**：实体链接索引 `build_index()` 要扫一遍图
   （5,696 个药 + 14,911 个病），**每个请求重建一次会让接口慢到不可用**；
   few-shot 示例也要读一次 CSV。这两样用锁保护，只建一次；
3. 把 `run_turn()` 的输出**裁成接口契约**（原始输出里有归因用的中间量，不该外泄）。

## 一个必须说清的边界

`run_turn` 的返回里 `result_ok` / `step` 这类字段是**评测归因**用的
（要和金标准比）；线上问答没有金标准，所以这些字段在接口里**不出现**。
线上只返回"答了什么、依据什么"，不返回"答得对不对"——**系统不该自评**。
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

#: backend/app/l5_service.py → backend/app → backend → 仓库根
REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = REPO_ROOT / "scripts"
for _p in (str(_SCRIPTS), str(REPO_ROOT / "backend")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from insert_io import read_csv_rows  # noqa: E402
from l5_entity_linking import build_index  # noqa: E402
from l5_pipeline import run_turn  # noqa: E402

FEWSHOT_FILE = REPO_ROOT / "data" / "eval" / "l5_fewshot.csv"

#: 进程级缓存。键：names / prefixes / max_len / fewshot
_state: dict = {}
_lock = threading.Lock()
_client = None


def _settings():
    from app.config import get_settings
    return get_settings()


def _llm_client(st):
    """LLM 客户端复用（创建开销不大，但没必要每请求一个连接池）。"""
    global _client
    if _client is None:
        from openai import OpenAI
        _client = OpenAI(api_key=st.dashscope_api_key,
                         base_url=st.dashscope_base_url,
                         timeout=60.0, max_retries=3)
    return _client


def _load_fewshot() -> list[dict]:
    if not FEWSHOT_FILE.exists():
        return []
    rows, _ = read_csv_rows(FEWSHOT_FILE)
    out = []
    for r in rows:
        out.append({
            "intent": (r.get("intent") or "").strip(),
            "question": (r.get("question") or "").strip(),
            "entities": [x.strip() for x in (r.get("entities") or "").split("；")
                         if x.strip()],
            "cypher": (r.get("cypher") or "").strip(),
        })
    return out


def _ensure_state(session) -> dict:
    """建一次索引与 few-shot；之后所有请求复用。

    ⚠️ 加锁：FastAPI 的同步路由跑在线程池里，**并发首请求会同时进来**，
    没有锁就会重复扫图（慢且浪费内存）。
    """
    if _state:
        return _state
    with _lock:
        if _state:                      # 双检：等锁期间别人可能已经建好
            return _state
        names, prefixes, aliases = build_index(session)
        _state.update({
            "names": names,
            "prefixes": prefixes,
            "aliases": aliases,
            "max_len": max((len(x) for x in names), default=2),
            "fewshot": _load_fewshot(),
        })
        return _state


def reload_index() -> None:
    """清掉缓存（图谱变更或测试用）。下次请求会重建。"""
    with _lock:
        _state.clear()


def _to_transcript(history) -> list[tuple[str, str]] | None:
    """把「问答对」转成 `rewrite_prompt` 要的**带角色对话记录**。

    ⚠️ **这里踩过一个坑，值得记下**：
    `l5_rewrite.rewrite_prompt(history, question)` 里的 `history` 是
    `[('user', 内容), ('assistant', 内容), …]`——**每个元素带角色**。
    我第一版直接传了 `[(用户问题, 系统回答)]` 这种二元组，结果
    `rewrite_prompt` 按 `role == 'user'` 判断，把**用户那句话渲染成了"助手：…"**，
    模型看到的所有轮次都是"助手说的"，自然没法把「它」补成药名——
    实测「它和依度沙班能一起吃吗？」被改写成「**这个药**和依度沙班能一起吃吗？」，
    实体只认出依度沙班，答成了另一个问题。

    **适配层要负责形状转换**：调用方给数据库里的形状（问答对），
    到这里再翻成脚本内部要的形状（角色化 transcript）。
    """
    if not history:
        return None
    out: list[tuple[str, str]] = []
    for item in history:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            continue
        q, a = str(item[0] or ""), str(item[1] or "")
        if q:
            out.append(("user", q))
        if a:
            out.append(("assistant", a))
    return out or None


def ask(question: str, history: list[tuple[str, str]] | None = None,
        *, do_rewrite: bool = True, trace: bool = False) -> dict:
    """跑一轮问答，返回**接口契约**（不含评测归因字段）。

    `history` 是 `[(用户问题, 系统回答), ...]`（按时间正序，与数据库里的形状一致）；
    内部会翻成 `rewrite_prompt` 要的角色化 transcript（见 `_to_transcript`）。
    多轮改写靠它把指代补全（决策 #39：一律改写）。
    """
    from neo4j import GraphDatabase

    st = _settings()
    if not st.llm_configured:
        raise RuntimeError("LLM 未配置（缺 DASHSCOPE_API_KEY / DASHSCOPE_BASE_URL）")

    driver = GraphDatabase.driver(st.neo4j_uri,
                                 auth=(st.neo4j_user, st.neo4j_password))
    try:
        with driver.session() as session:
            state = _ensure_state(session)
            out = run_turn(
                session, _llm_client(st), st, question,
                history=_to_transcript(history),
                names=state["names"], prefixes=state["prefixes"],
            aliases=state["aliases"],
                max_len=state["max_len"], fewshot=state["fewshot"],
                do_rewrite=do_rewrite, trace=trace,
            )
    finally:
        driver.close()

    rows = out.get("rows") or []
    # ⚠️ 只返回**前 20 行**预览：有的查询会返回上百行（如"某药出现在 137 个疾病里"），
    #    整包塞进 JSON 会让响应变得很大，而界面上也读不完。
    return {
        "question": out.get("question", question),
        "rewritten": out.get("rewritten", question),
        "intent": out.get("intent"),
        "entities": out.get("entities") or [],
        "step": out.get("step"),
        "answer": out.get("answer", ""),
        "cypher": out.get("cypher"),
        "row_count": len(rows),
        "rows_preview": rows[:20],
        "evidence": out.get("evidence"),
        "evidence_consistent": out.get("evidence_consistent"),
        "faithful": out.get("faithful"),
        "out_of_evidence": (out.get("out_of_evidence") or [])[:10],
    }
