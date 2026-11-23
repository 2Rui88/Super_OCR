"""项目一 OCR 服务客户端 —— 异步提交 + 按契约退避轮询。

对接文档：01-OCR流水线/服务/docs/API接口文档.md（契约冻结）。
WORKER_LOST / SERVICE_RESTARTED 属可重试错误：重新提交（带上限）。
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from uuid import uuid4

import httpx

from app.config import settings

log = logging.getLogger("kb.p1")

_RETRYABLE_ERROR_CODES = {"WORKER_LOST", "SERVICE_RESTARTED"}


class P1Error(RuntimeError):
    def __init__(self, error_code: str, message: str):
        super().__init__(f"{error_code}: {message}")
        self.error_code = error_code


class P1TaskError(P1Error):
    """查询已确认任务失败或不存在，可以丢弃旧任务号。"""


async def recognize_pdf(
    pdf_path: str, *, task_id: int | None = None, request_key: str | None = None,
    save_submission: Callable[[str | None, int | None], None] | None = None,
    on_phase=None, alive=None,
) -> dict:
    """恢复已有 OCR 任务，或先保存提交键再 POST。返回内层 DocumentResult。"""
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
        resubmits = 0
        while True:
            _require_lease(alive)
            if task_id is None:
                if request_key is None:
                    request_key = uuid4().hex
                    if save_submission is not None:
                        save_submission(request_key, None)
                task_id = await _submit(client, pdf_path, request_key)
                if save_submission is not None:
                    save_submission(request_key, task_id)
            try:
                return await _poll(client, task_id, on_phase=on_phase, alive=alive)
            except P1TaskError as exc:
                # 网络异常和查询超时无法确定远端终态，保留原任务号与提交键。
                if save_submission is not None:
                    save_submission(None, None)
                if exc.error_code not in _RETRYABLE_ERROR_CODES or resubmits >= settings.p1_resubmit_max:
                    raise
                resubmits += 1
                log.warning("P1 任务 #%s %s，第 %d 次重新提交", task_id, exc.error_code, resubmits)
                task_id, request_key = None, None
                await asyncio.sleep(2 * resubmits)


def _require_lease(alive) -> None:
    if alive is not None and not alive():
        raise P1Error("LEASE_LOST", "文档租约已丢失，停止 OCR 请求")


async def _submit(client: httpx.AsyncClient, pdf_path: str, request_key: str) -> int:
    with open(pdf_path, "rb") as fh:
        resp = await client.post(
            f"{settings.p1_base_url}/documents",
            files={"file": ("doc.pdf", fh, "application/pdf")},
            headers={"Idempotency-Key": request_key},
        )
    if resp.status_code == 413:
        raise P1Error("FILE_TOO_LARGE", "超过项目一上传上限")
    resp.raise_for_status()
    return resp.json()["task_id"]


async def _poll(client: httpx.AsyncClient, task_id: int, *, on_phase=None, alive=None) -> dict:
    delay = settings.p1_poll_start_seconds
    waited = 0.0
    while True:
        _require_lease(alive)
        resp = await client.get(f"{settings.p1_base_url}/tasks/{task_id}")
        if resp.status_code == 404:
            raise P1TaskError("TASK_VANISHED", f"任务 #{task_id} 不存在（可能被人工清理）")
        resp.raise_for_status()
        task = resp.json()
        status = task["status"]
        if status == "done":
            envelope = task["result"]
            return envelope["result"]
        if status == "failed":
            raise P1TaskError(task.get("error_code") or "WORKER_ERROR",
                              task.get("error_message") or "识别失败")
        if on_phase and task.get("phase"):
            on_phase(task["phase"])
        if waited >= settings.p1_poll_timeout_seconds:
            raise P1Error("POLL_TIMEOUT", f"等待任务 #{task_id} 超时")
        await asyncio.sleep(delay)
        waited += delay
        delay = min(delay * 1.5, settings.p1_poll_max_seconds)
