"""任务接口 —— 最近证件图片、轮询详情、单张重试。"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Query, Request

from app.core.task_store import MAX_TASK_ATTEMPTS, PERMANENT_ERROR_CODES

router = APIRouter(tags=["tasks"])


@router.get("/tasks/recent")
def recent_cards(request: Request, limit: int = Query(20, ge=1, le=50)):
    items = []
    for task in request.app.state.store.recent_card_tasks(limit):
        try:
            envelope = json.loads(task.pop("result_json"))
        except json.JSONDecodeError:
            continue
        if not isinstance(envelope, dict) or envelope.get("kind") not in ("card", "document"):
            continue
        result = envelope.get("result")
        if not isinstance(result, dict):
            continue
        is_card = envelope["kind"] == "card"
        items.append({
            **task,
            "type_cn": (result.get("doc_type_cn") or "证件") if is_card else "文档图片",
            "issues": sum(bool(f.get("needs_review")) for f in result.get("fields", [])) if is_card else 0,
            "duration_ms": result.get("duration_ms" if is_card else "total_duration_ms"),
        })
    return {"items": items}


@router.get("/tasks/{task_id}")
def get_task(task_id: int, request: Request):
    task = request.app.state.store.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail={
            "error_code": "TASK_NOT_FOUND", "message": f"任务 {task_id} 不存在"})
    return {
        "id": task["id"],
        "task_type": task["task_type"],
        "status": task["status"],                # queued | running | done | failed
        "phase": task["phase"],
        "progress": task["progress"],
        "pages_progress": json.loads(task["pages_progress"]) if task["pages_progress"] else None,
        "filename": task["filename"],
        "engine_type": task["engine_type"],      # card_ocr | structure_v3
        "attempts": task["attempts"],
        "worker_id": task["worker_id"],
        "error_code": task["error_code"],
        "error_message": task["error_message"],
        "created_at": task["created_at"],
        "started_at": task["started_at"],
        "finished_at": task["finished_at"],
        "result": json.loads(task["result_json"]) if task["result_json"] else None,
    }


@router.post("/tasks/{task_id}/retry")
def retry_task(task_id: int, request: Request):
    """单张重试：只对 failed 且未到重试上限/非永久失败的任务生效。"""
    task = request.app.state.store.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail={
            "error_code": "TASK_NOT_FOUND", "message": f"任务 {task_id} 不存在"})
    if task["status"] != "failed":
        raise HTTPException(status_code=409, detail={
            "error_code": "NOT_RETRYABLE", "message": f"任务状态为 {task['status']}，只有失败任务可重试"})
    if task["error_code"] in PERMANENT_ERROR_CODES or task["attempts"] >= MAX_TASK_ATTEMPTS:
        raise HTTPException(status_code=409, detail={
            "error_code": "PERMANENT_FAILURE",
            "message": "永久失败（重试到顶或不可重试错误），请检查文件后重新上传"})
    if not request.app.state.store.requeue_task(task_id):
        raise HTTPException(status_code=409, detail={
            "error_code": "NOT_RETRYABLE", "message": "重排队失败，请稍后重试"})
    return {"task_id": task_id, "status": "queued"}
