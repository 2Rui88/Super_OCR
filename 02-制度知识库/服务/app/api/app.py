"""FastAPI 入口 —— lifespan 启停消费循环；路由按资源分文件。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import worker
from app.api import routes_documents, routes_eval, routes_qa, routes_queue
from app.db import init_db

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    worker.start_worker()
    try:
        yield
    finally:
        await worker.stop_worker()


app = FastAPI(title="制度知识库", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 开发期；部署收敛到同源
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(routes_documents.router, prefix="/api/v1")
app.include_router(routes_queue.router, prefix="/api/v1")
app.include_router(routes_qa.router, prefix="/api/v1")
app.include_router(routes_eval.router, prefix="/api/v1")

_STATIC = Path(__file__).resolve().parents[1] / "static"
app.mount("/static", StaticFiles(directory=_STATIC), name="static")


@app.get("/")
def index():
    # 样式迭代频繁:HTML 禁缓存,保证刷新即拿到最新版(资源类文件仍走正常缓存)
    return FileResponse(_STATIC / "index.html",
                        headers={"Cache-Control": "no-cache, must-revalidate"})
