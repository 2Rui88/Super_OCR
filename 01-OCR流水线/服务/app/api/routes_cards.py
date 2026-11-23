"""POST /api/v1/cards —— 证件（图片）识别。

单张图片可同步等待（默认最多 settings.sync_wait_seconds 秒）：
完成 → 200 + 统一信封；超时 → 202 + task_id（任务继续跑，轮询即可）。
wait=false 直接返回 202。识别永远发生在 worker 进程，API 不加载模型。
"""
from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse

from app.api.uploads import IMAGE_EXTENSIONS, read_bounded, save_upload

router = APIRouter(tags=["cards"])


@router.post("/cards")
async def recognize_card(
    request: Request,
    file: UploadFile = File(...),
    type: str | None = Form(default=None, description="证件类型提示（可选）"),
    wait: bool = Form(default=True, description="是否同步等待结果"),
):
    settings = request.app.state.settings
    store = request.app.state.store
    content = await read_bounded(file, settings.max_upload_mb)
    digest, path = save_upload(content, file.filename or "upload",
                               allowed=IMAGE_EXTENSIONS,
                               upload_dir=settings.upload_dir,
                               max_mb=settings.max_upload_mb)
    task_id = store.create_task(task_type="card", filename=file.filename or path.name,
                                file_sha256=digest, file_path=path, hint_type=type)
    if not wait:
        return JSONResponse(status_code=202,
                            content={"task_id": task_id, "status": "queued"})

    deadline = time.monotonic() + settings.sync_wait_seconds
    task = None
    while time.monotonic() < deadline:
        task = store.get_task(task_id)
        if task["status"] in ("done", "failed"):
            break
        await asyncio.sleep(0.5)
    if task is None or task["status"] not in ("done", "failed"):
        return JSONResponse(status_code=202, content={
            "task_id": task_id, "status": "running",
            "message": "识别仍在进行，请轮询 GET /api/v1/tasks/{task_id}",
        })
    if task["status"] == "done":
        return {"task_id": task_id, **json.loads(task["result_json"])}
    return JSONResponse(status_code=422, content={
        "task_id": task_id, "status": "failed",
        "error_code": task["error_code"], "error_message": task["error_message"],
    })
