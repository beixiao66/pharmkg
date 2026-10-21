"""问答接口（把 L5 流水线暴露成 HTTP）。

## 为什么是 `/ask` 而不是塞进 `/conversations/{id}/messages`

会话与消息的持久化由 L7 的 `/conversations` 管；这里是**纯粹的一问一答**。
让前端"先拿答案、再决定要不要存"，比在 `/ask` 里顺手写库更干净：

* 存不存、存到哪个会话，是**产品策略**，应该由调用方决定；
* 写库失败不该影响"答案已经算出来"这件事；
* 这两个接口各自可测，不用互相打桩。

## 一个刻意的设计：接口返回**证据与 Cypher**

提案里说"每条回答都能展开看实际执行的 Cypher 和命中记录——可解释性是可验证的"。
所以 `cypher`、`row_count`、`rows_preview`、`evidence` 都要返回，
而且**不返回"答得对不对"**——那是评测（L9）的事，线上系统不该自评。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.deps import get_current_user
from app.models import AppUser
from app.schemas import AskRequest, AskResponse

router = APIRouter(tags=["问答"])
log = logging.getLogger(__name__)


@router.post("/ask", response_model=AskResponse,
             summary="自然语言问答（意图→实体→Cypher→执行→证据→答案）")
def ask(body: AskRequest,
        user: AppUser = Depends(get_current_user)) -> AskResponse:
    """跑一轮 L5 问答。

    ⚠️ **可能慢**：首次请求要建实体链接索引（扫一遍图，实测约 3.4 秒），
    之后复用到进程生命周期结束；单次问答实测 1.5–4 秒（LLM 调用占大头）。
    """
    from app import l5_service

    try:
        out = l5_service.ask(
            body.question,
            # 与数据库里的形状一致：[{"question": ..., "answer": ...}, ...]
            history=[(h.question, h.answer) for h in (body.history or [])],
            do_rewrite=body.do_rewrite,
        )
    except RuntimeError as exc:            # LLM 未配置
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        # ⚠️ 不把堆栈抛给前端，但要留日志——否则线上出错只剩一句"服务端出错"
        log.exception("L5 问答失败")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail=f"问答执行失败（{type(exc).__name__}）") from exc

    return AskResponse(**out)


@router.post("/ask/reload-index", summary="重建实体链接索引（图谱变更后用）")
def reload_index(user: AppUser = Depends(get_current_user)) -> dict:
    """清缓存，下次 `/ask` 会重建。图谱重导之后必须调一次，否则还会用旧名字。"""
    from app import l5_service
    l5_service.reload_index()
    return {"reloaded": True}
