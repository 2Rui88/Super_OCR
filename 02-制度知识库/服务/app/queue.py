"""摄入队列 —— documents 行即租约（心跳租约定稿设计）。

四要素：
- 领单：条件 UPDATE 抢占（候选 LIMIT 3，落空换下一个），rowcount=1 即提交；
- 心跳：每次领单的令牌独立，三态 renewed/finished/lost；
- 回收：判据是"5 分钟无心跳"而非"超时"——慢任务活着永不误杀；
  条件 UPDATE 幂等，任何实例执行都安全，无启动清扫；
- 终态：仅持有者可写；丢租约者影响 0 行，不踩新持有者。
纯基础设施，不 import 摄入业务代码。
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import uuid4

from app.config import settings
from app.db import connect

# 租约三态
LEASE_RENEWED = "renewed"
LEASE_FINISHED = "finished"
LEASE_LOST = "lost"

# 入队结果
QUEUED = "queued"          # 新任务


def _now() -> int:
    return int(time.time())


@dataclass
class ClaimedDoc:
    id: int
    filename: str
    fmt: str
    sha256: str
    pdf_path: str | None
    p1_result_json: str | None
    worker: str
    lease_token: str
    p1_request_key: str | None
    p1_task_id: int | None


class LeaseLost(RuntimeError):
    """该次领单已被重置或回收。"""


_OWNED = "id=? AND status='processing' AND lease_token=?"


@contextmanager
def lease_write(doc_id: int, lease_token: str):
    """在同一写事务内验证租约并修改文档派生数据。"""
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute(f"SELECT 1 FROM documents WHERE {_OWNED}",
                      (doc_id, lease_token)).fetchone() is None:
            raise LeaseLost("文档租约已丢失")
        yield db


def enqueue(filename: str, fmt: str, sha256: str, *,
            pdf_path: str | None = None) -> tuple[str, int]:
    """为每次导入创建独立的待处理文档。"""
    with connect() as db:
        cur = db.execute(
            "INSERT INTO documents (filename, fmt, sha256, status, pdf_path, created_at)"
            " VALUES (?,?,?,'pending',?,?)",
            (filename, fmt, sha256, pdf_path, _now()))
        return QUEUED, cur.lastrowid


def claim_next(worker_id: str) -> ClaimedDoc | None:
    """抢占一个 pending：候选最多 3 个，被抢走换下一个。"""
    with connect() as db:
        candidates = db.execute(
            "SELECT id FROM documents WHERE status='pending' ORDER BY id LIMIT 3"
        ).fetchall()
        for cand in candidates:
            lease_token = uuid4().hex
            row = db.execute(
                "UPDATE documents SET status='processing', worker=?, lease_token=?, heartbeat_at=?,"
                " attempts=attempts+1, error='', phase=''"
                " WHERE id=? AND status='pending'"
                " RETURNING id, filename, fmt, sha256, pdf_path, p1_result_json,"
                "           worker, lease_token, p1_request_key, p1_task_id",
                (worker_id, lease_token, _now(), cand["id"])).fetchone()
            if row is not None:
                return ClaimedDoc(**dict(row))
        return None


def renew_lease(doc_id: int, lease_token: str) -> str:
    """心跳续租：仅持有者生效。三态见模块头。"""
    with connect() as db:
        row = db.execute(
            f"UPDATE documents SET heartbeat_at=? WHERE {_OWNED} RETURNING id",
            (_now(), doc_id, lease_token)).fetchone()
        if row is not None:
            return LEASE_RENEWED
        status = db.execute("SELECT status FROM documents WHERE id=?", (doc_id,)).fetchone()
        if status is None:
            return LEASE_LOST
        return LEASE_FINISHED if status["status"] in ("ready", "failed") else LEASE_LOST


def reclaim_stale(timeout_seconds: int | None = None,
                  max_attempts: int | None = None) -> int:
    """回收无心跳的行。有 OCR 检查点时继续本次尝试，其他行按次数回收。"""
    timeout = timeout_seconds or settings.lease_timeout_seconds
    maxa = max_attempts or settings.max_attempts
    now = _now()
    has_checkpoint = (
        "p1_request_key IS NOT NULL OR p1_task_id IS NOT NULL OR p1_result_json IS NOT NULL"
    )
    with connect() as db:
        cur = db.execute(
            "UPDATE documents SET"
            f"  status=CASE WHEN attempts < ? OR ({has_checkpoint}) THEN 'pending' ELSE 'failed' END,"
            f"  attempts=CASE WHEN ({has_checkpoint}) THEN MAX(0, attempts-1) ELSE attempts END,"
            "   error=?, worker='', lease_token=NULL, phase='', heartbeat_at=0,"
            f"  finished_at=CASE WHEN attempts >= ? AND NOT ({has_checkpoint}) THEN ? ELSE NULL END"
            " WHERE status='processing' AND heartbeat_at < ?",
            (maxa, f"超时回收（心跳超过 {timeout} 秒无更新）",
             maxa, now, now - timeout))
        return cur.rowcount


def release_lease(doc_id: int, lease_token: str) -> None:
    """正常停机归还任务，保留 OCR 检查点，撤销本次领单计数。"""
    with connect() as db:
        db.execute(
            "UPDATE documents SET status='pending', worker='', lease_token=NULL,"
            " phase='', heartbeat_at=0, attempts=MAX(0, attempts-1)"
            f" WHERE {_OWNED}", (doc_id, lease_token))


def set_phase(doc_id: int, lease_token: str, phase: str) -> None:
    """推进阶段描述（顺带续租：同样的持有者条件）。"""
    with connect() as db:
        updated = db.execute(
            f"UPDATE documents SET phase=?, heartbeat_at=? WHERE {_OWNED}",
            (phase, _now(), doc_id, lease_token))
        if updated.rowcount != 1:
            raise LeaseLost("文档租约已丢失")


def save_submission(doc_id: int, lease_token: str, pdf_path: str,
                    request_key: str | None, task_id: int | None) -> None:
    """保存提交键和已知任务号，或清除查询已确认结束的失败任务。"""
    with lease_write(doc_id, lease_token) as db:
        db.execute(
            "UPDATE documents SET pdf_path=?, p1_request_key=?, p1_task_id=? WHERE id=?",
            (pdf_path, request_key, task_id, doc_id))


def save_ocr_result(doc_id: int, lease_token: str, result_json: str) -> None:
    """在切块和向量化之前持久化识别结果。"""
    with lease_write(doc_id, lease_token) as db:
        db.execute("UPDATE documents SET p1_result_json=? WHERE id=?", (result_json, doc_id))


def finish(doc_id: int, lease_token: str, *, ok: bool, error: str = "",
           counts: tuple[int, int, int] | None = None) -> str | None:
    """收敛终态（仅持有者可写）。丢租约返回 None，本次执行作废。

    就绪时写入解析统计。
    """
    err = (error or "")[:500]
    with connect() as db:
        if ok:
            row = db.execute(
                "UPDATE documents SET status='ready', error='', worker='', lease_token=NULL, phase='',"
                " finished_at=?, page_count=?, table_count=?, figure_count=?"
                f" WHERE {_OWNED} RETURNING id",
                (_now(), *(counts or (0, 0, 0)), doc_id, lease_token)).fetchone()
            if row is None:
                return None
            return "ready"
        row = db.execute(
            "UPDATE documents SET"
            "   status=CASE WHEN attempts < ? THEN 'pending' ELSE 'failed' END,"
            "   error=?, worker='', lease_token=NULL, phase='',"
            "   finished_at=CASE WHEN attempts >= ? THEN ? ELSE finished_at END"
            f" WHERE {_OWNED} RETURNING id, status",
            (settings.max_attempts, err, settings.max_attempts, _now(), doc_id, lease_token)
        ).fetchone()
        return row["status"] if row else None


def holds_lease(doc_id: int, lease_token: str) -> bool:
    """租约检查：该行是否仍由本持有者持有（供长轮询期间随时自检）。"""
    with connect() as db:
        row = db.execute(f"SELECT 1 FROM documents WHERE {_OWNED}",
                         (doc_id, lease_token)).fetchone()
    return row is not None


def reset_for_reparse(doc_id: int) -> bool:
    """重新解析：重置行（含处理中——旧执行者将由租约闭环自动止损）。"""
    with connect() as db:
        cur = db.execute(
            "UPDATE documents SET status='pending', worker='', lease_token=NULL, attempts=0, error='',"
            " phase='', heartbeat_at=0 WHERE id=?", (doc_id,))
        return cur.rowcount == 1


def stats() -> dict:
    """队列水位：各状态计数 + 最老 pending 年龄 + 处理中持有者数。"""
    with connect() as db:
        counts = {row["status"]: row["c"] for row in db.execute(
            "SELECT status, COUNT(*) c FROM documents GROUP BY status")}
        oldest = db.execute(
            "SELECT MIN(created_at) m FROM documents WHERE status='pending'").fetchone()
        workers = db.execute(
            "SELECT COUNT(DISTINCT worker) c FROM documents"
            " WHERE status='processing' AND worker != ''").fetchone()
    now = _now()
    return {
        "pending": counts.get("pending", 0),
        "processing": counts.get("processing", 0),
        "ready": counts.get("ready", 0),
        "failed": counts.get("failed", 0),
        "oldest_pending_seconds": max(0, now - oldest["m"]) if oldest and oldest["m"] else 0,
        "active_workers": workers["c"] if workers else 0,
    }
