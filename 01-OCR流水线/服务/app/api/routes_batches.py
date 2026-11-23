"""批量接口 —— 上传、批次列表、进度、重试（批次级/单张）、指定类型重识别、导出。"""
from __future__ import annotations

from pathlib import Path
from typing import List

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.api.exporting import export_batch
from app.api.uploads import (DOCUMENT_EXTENSIONS, IMAGE_EXTENSIONS,
                             read_bounded, save_upload)

router = APIRouter(tags=["batches"])

#: 指定类型重识别接受的证件类型（与证件注册表一致）
CERT_TYPES = ("id_front", "id_back", "license", "driver")

#: 批量接口统一白名单：证件图片 → card 任务，PDF/Office → document_scan 任务
BATCH_ALLOWED = IMAGE_EXTENSIONS | DOCUMENT_EXTENSIONS


def _batch_or_404(request: Request, batch_id: int):
    batch = request.app.state.store.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail={
            "error_code": "BATCH_NOT_FOUND", "message": f"批次 {batch_id} 不存在"})
    return batch


@router.get("/batches")
def list_batches(request: Request):
    """批次下拉列表：最近 50 个批次及状态概览。"""
    return request.app.state.store.list_batches()


@router.post("/batches", status_code=202)
async def create_batch(request: Request, files: List[UploadFile] = File(...)):
    """一次传多个证件图片或 PDF/Office 文档，按扩展名分流任务类型。
    单个文件不合法不影响其他文件：非法文件落为永久失败的批次项（带原因），
    合法文件正常排队。"""
    settings = request.app.state.settings
    store = request.app.state.store
    task_ids: list[int] = []
    rejected = 0
    for upload in files:
        name = upload.filename or "upload"
        ext = Path(name).suffix.lower()
        task_type = "document_scan" if ext in DOCUMENT_EXTENSIONS else "card"
        try:
            content = await read_bounded(upload, settings.max_upload_mb)
            digest, path = save_upload(content, name,
                                       allowed=BATCH_ALLOWED,
                                       upload_dir=settings.upload_dir,
                                       max_mb=settings.max_upload_mb)
            task_ids.append(store.create_task(
                task_type=task_type, filename=name,
                file_sha256=digest, file_path=path))
        except HTTPException as exc:  # 上传校验失败 → 永久失败占位，隔离于其他文件
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            rejected += 1
            task_ids.append(store.create_failed_task(
                task_type=task_type, filename=name,
                error_code=detail.get("error_code", "UNSUPPORTED_FORMAT"),
                error_message=detail.get("message", str(exc.detail))))
    batch_id = store.create_batch(task_ids)
    return JSONResponse(status_code=202, content={
        "batch_id": batch_id, "total": len(task_ids),
        "accepted": len(task_ids) - rejected, "rejected": rejected,
        "message": "请轮询 GET /api/v1/batches/{batch_id} 查看逐文件进度",
    })


@router.get("/batches/{batch_id}")
def get_batch(batch_id: int, request: Request):
    return _batch_or_404(request, batch_id)


@router.post("/batches/{batch_id}/retry_failed")
def retry_failed(batch_id: int, request: Request):
    _batch_or_404(request, batch_id)
    return request.app.state.store.retry_failed(batch_id)


class RetypeBody(BaseModel):
    type: str


@router.post("/batches/{batch_id}/items/{task_id}/retype")
def retype_item(batch_id: int, task_id: int, body: RetypeBody, request: Request):
    """指定类型重识别：对已落盘文件带 hint 新建任务，批次项改指新任务。

    用于"不是支持的证件类型"（路由成文档）的行——用户明确告诉系统这是哪类证件。
    """
    _batch_or_404(request, batch_id)
    if body.type not in CERT_TYPES:
        raise HTTPException(status_code=422, detail={
            "error_code": "BAD_CERT_TYPE",
            "message": f"type 必须是 {' / '.join(CERT_TYPES)}"})
    store = request.app.state.store
    task = store.get_task(task_id)
    if task is None or not Path(task["file_path"]).is_file():
        raise HTTPException(status_code=404, detail={
            "error_code": "FILE_NOT_FOUND", "message": "任务或原件不存在"})
    if task["task_type"] != "card":
        raise HTTPException(status_code=422, detail={
            "error_code": "NOT_A_CARD_TASK",
            "message": "指定类型重识别只适用于证件任务"})
    new_task_id = store.create_task(
        task_type="card", filename=task["filename"],
        file_sha256=task["file_sha256"], file_path=task["file_path"],
        hint_type=body.type)
    if not store.replace_batch_item(batch_id, task_id, new_task_id):
        raise HTTPException(status_code=404, detail={
            "error_code": "ITEM_NOT_FOUND", "message": f"任务 {task_id} 不在批次 {batch_id} 中"})
    return {"task_id": new_task_id, "status": "queued", "hint_type": body.type}


@router.get("/batches/{batch_id}/export")
def export(batch_id: int, request: Request, format: str = "json"):
    store = request.app.state.store
    return export_batch(_batch_or_404(request, batch_id),
                        store.batch_tasks(batch_id), format)
