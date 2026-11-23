"""GET /api/v1/health —— 分层健康检查。

API 进程只做便宜检查：SQLite 可写、任务表可访问、worker 心跳新鲜度
（模型是否已加载看 worker 上报的标志，API 永不加载模型）。
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(tags=["health"])


@router.get("/health")
def health(request: Request):
    settings = request.app.state.settings
    store = request.app.state.store
    sqlite_ok = store.healthcheck()
    workers = store.list_workers(settings.worker_stale_seconds) if sqlite_ok else []
    alive = [w for w in workers if w["alive"]]
    status = "ok" if sqlite_ok and alive else ("degraded" if sqlite_ok else "error")
    return JSONResponse(
        status_code=200 if status != "error" else 503,
        content={
            "status": status,
            "components": {
                "sqlite_writable": sqlite_ok,
                "workers": [
                    {
                        "worker_id": w["worker_id"],
                        "alive": w["alive"],
                        "card_model_loaded": bool(w["card_model_loaded"]),
                        "document_model_loaded": bool(w["document_model_loaded"]),
                        "current_task_id": w["current_task_id"],
                    }
                    for w in workers
                ],
            },
        },
    )
