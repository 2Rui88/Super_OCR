"""FastAPI 应用工厂 —— 只做装配：路由注册、目录初始化、任务库挂载、前端静态页。"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import (routes_batches, routes_cards, routes_certs, routes_documents,
                     routes_eval, routes_files, routes_health, routes_tasks)
from app.config import Settings, settings

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"


def create_app(config: Settings | None = None) -> FastAPI:
    from app.core.task_store import TaskStore

    config = config or settings
    app = FastAPI(title="OCR 流水线服务", version="1.0.0")
    app.state.settings = config
    app.state.store = TaskStore(config.task_db_path)
    config.upload_dir.mkdir(parents=True, exist_ok=True)
    for router in (routes_health.router, routes_cards.router,
                   routes_certs.router, routes_documents.router,
                   routes_tasks.router, routes_batches.router,
                   routes_eval.router, routes_files.router):
        app.include_router(router, prefix="/api/v1")
    if FRONTEND_DIR.is_dir():  # 前端静态页挂在根路径；/api/* 路由优先
        app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
    return app


app = create_app()  # uvicorn app.api.app:app
