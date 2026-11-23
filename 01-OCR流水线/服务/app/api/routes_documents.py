"""POST /api/v1/documents —— 文档识别，始终异步返回 task_id。

接受 PDF 与 Office 六格式（doc/docx/xls/xlsx/ppt/pptx）。Office 文件由
worker 先转 PDF 再识别（LibreOffice，见 app/core/doc_convert.py）；所有
文档统一逐页走 PP-StructureV3，任务类型固定 document_scan，上传端不再
做逐页文字层预检。
"""
from __future__ import annotations

from fastapi import APIRouter, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from app.api.uploads import DOCUMENT_EXTENSIONS, read_bounded, save_upload
from app.core.task_store import IdempotencyConflict

router = APIRouter(tags=["documents"])


@router.post("/documents", status_code=202)
async def recognize_document(
    request: Request,
    file: UploadFile = File(...),
    idempotency_key: str | None = Header(default=None, min_length=1, max_length=128),
):
    settings = request.app.state.settings
    store = request.app.state.store
    content = await read_bounded(file, settings.max_upload_mb)
    digest, path = save_upload(content, file.filename or "upload.pdf",
                               allowed=DOCUMENT_EXTENSIONS,
                               upload_dir=settings.upload_dir,
                               max_mb=settings.max_upload_mb)
    try:
        task_id = store.create_task(task_type="document_scan",
                                    filename=file.filename or path.name,
                                    file_sha256=digest, file_path=path,
                                    idempotency_key=idempotency_key)
    except IdempotencyConflict as exc:
        raise HTTPException(409, detail={
            "error_code": "IDEMPOTENCY_CONFLICT", "message": str(exc),
        }) from exc
    return JSONResponse(status_code=202, content={
        "task_id": task_id, "status": "queued",
        "message": "文档识别为异步任务，请轮询 GET /api/v1/tasks/{task_id}",
    })
