"""摄入队列 —— documents 行即租约（心跳租约定稿设计）。

四要素：
- 领单：条件 UPDATE 抢占（候选 LIMIT 3，落空换下一个），rowcount=1 即提交；
- 心跳：仅持有者可续（WHERE 带 worker 匹配），三态 renewed/finished/lost；
- 回收：判据是"5 分钟无心跳"而非"超时"——慢任务活着永不误杀；
  条件 UPDATE 幂等，任何实例执行都安全，无启动清扫；
- 终态：仅持有者可写；丢租约者影响 0 行，不踩新持有者。
纯基础设施，不 import 摄入业务代码。
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from app.config import settings
from app.db import connect

# 租约三态
LEASE_RENEWED = "renewed"
LEASE_FINISHED = "finished"
LEASE_LOST = "lost"

# 入队结果
QUEUED = "queued"          # 新任务
REQUEUED = "requeued"      # failed 行被重置（手动重试/重新解析）
INFLIGHT = "inflight"      # 同内容正在处理，幂等复用
DUPLICATE = "duplicate"    # 同内容已就绪，不重复导入


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
    replaces_target: int | None
    worker: str = ""


def enqueue(filename: str, fmt: str, sha256: str, *, pdf_path: str | None = None,
            replaces_target: int | None = None) -> tuple[str, int]:
    """幂等入队。返回 (结果, doc_id)。

    同内容在途 → inflight（复用）；已就绪 → duplicate（是否替代由调用方在上层
    用 replaces_target 建新行决定，这里不悄悄改旧行）；failed → 重置重投。
    """
    with connect() as db:
        row = db.execute(
            "SELECT id, status FROM documents WHERE sha256=? ORDER BY id DESC LIMIT 1",
            (sha256,)).fetchone()
        if row is not None and row["status"] in ("pending", "processing"):
            return INFLIGHT, row["id"]
        if row is not None and row["status"] == "ready" and not replaces_target:
            return DUPLICATE, row["id"]
        if row is not None and row["status"] == "failed":
            db.execute(
                "UPDATE documents SET status='pending', attempts=0, error='', worker='',"
                " phase='', heartbeat_at=0, replaces_target=?, pdf_path=COALESCE(?, pdf_path)"
                " WHERE id=?", (replaces_target, pdf_path, row["id"]))
            return REQUEUED, row["id"]
        cur = db.execute(
            "INSERT INTO documents (filename, fmt, sha256, status, pdf_path,"
            " replaces_target, created_at) VALUES (?,?,?,'pending',?,?,?)",
            (filename, fmt, sha256, pdf_path, replaces_target, _now()))
        return QUEUED, cur.lastrowid


def claim_next(worker_id: str) -> ClaimedDoc | None:
    """抢占一个 pending：候选最多 3 个，被抢走换下一个。"""
    with connect() as db:
        candidates = db.execute(
            "SELECT id FROM documents WHERE status='pending' ORDER BY id LIMIT 3"
        ).fetchall()
        for cand in candidates:
            row = db.execute(
                "UPDATE documents SET status='processing', worker=?, heartbeat_at=?,"
                " attempts=attempts+1, error='', phase=''"
                " WHERE id=? AND status='pending'"
                " RETURNING id, filename, fmt, sha256, pdf_path, p1_result_json,"
                "           replaces_target",
                (worker_id, _now(), cand["id"])).fetchone()
            if row is not None:
                return ClaimedDoc(row["id"], row["filename"], row["fmt"], row["sha256"],
                                  row["pdf_path"], row["p1_result_json"],
                                  row["replaces_target"], worker_id)
        return None


def renew_lease(doc_id: int, worker: str) -> str:
    """心跳续租：仅持有者生效。三态见模块头。"""
    with connect() as db:
        row = db.execute(
            "UPDATE documents SET heartbeat_at=?"
            " WHERE id=? AND status='processing' AND worker=? RETURNING id",
            (_now(), doc_id, worker)).fetchone()
        if row is not None:
            return LEASE_RENEWED
        status = db.execute("SELECT status FROM documents WHERE id=?", (doc_id,)).fetchone()
        if status is None:
            return LEASE_LOST
        return LEASE_FINISHED if status["status"] in ("ready", "failed") else LEASE_LOST


def reclaim_stale(timeout_seconds: int | None = None,
                  max_attempts: int | None = None) -> int:
    """回收无心跳的处理中行：次数未超限回 pending 重投，超限落 failed。幂等。"""
    timeout = timeout_seconds or settings.lease_timeout_seconds
    maxa = max_attempts or settings.max_attempts
    with connect() as db:
        cur = db.execute(
            "UPDATE documents SET"
            "   status=CASE WHEN attempts < ? THEN 'pending' ELSE 'failed' END,"
            "   error=?, worker='', phase='',"
            "   finished_at=CASE WHEN attempts >= ? THEN ? ELSE finished_at END"
            " WHERE status='processing' AND heartbeat_at < ?",
            (maxa, f"超时回收（心跳超过 {timeout} 秒无更新）",
             maxa, _now(), _now() - timeout))
        return cur.rowcount


def set_phase(doc_id: int, worker: str, phase: str) -> None:
    """推进阶段描述（顺带续租：同样的持有者条件）。"""
    with connect() as db:
        db.execute(
            "UPDATE documents SET phase=?, heartbeat_at=?"
            " WHERE id=? AND status='processing' AND worker=?",
            (phase, _now(), doc_id, worker))


def finish(doc_id: int, worker: str, *, ok: bool, error: str = "",
           counts: tuple[int, int, int] | None = None,
           p1_result_json: str | None = None, pdf_path: str | None = None) -> str | None:
    """收敛终态（仅持有者可写）。丢租约返回 None，本次执行作废。

    就绪时顺带：写入统计、存档 JSON、落实"替代旧版本"（旧文档标 superseded_by）。
    """
    err = (error or "")[:500]
    with connect() as db:
        if ok:
            row = db.execute(
                "UPDATE documents SET status='ready', error='', worker='', phase='',"
                " finished_at=?, page_count=?, table_count=?, figure_count=?,"
                " p1_result_json=COALESCE(?, p1_result_json), pdf_path=COALESCE(?, pdf_path)"
                " WHERE id=? AND status='processing' AND worker=? RETURNING id",
                (_now(), *(counts or (0, 0, 0)), p1_result_json, pdf_path,
                 doc_id, worker)).fetchone()
            if row is None:
                return None
            # 替代旧版本：先读声明的目标，落实 superseded_by，再清声明位
            target = db.execute("SELECT replaces_target FROM documents WHERE id=?",
                                (doc_id,)).fetchone()
            if target and target["replaces_target"]:
                db.execute(
                    "UPDATE documents SET superseded_by=? WHERE id=? AND id != ?",
                    (doc_id, target["replaces_target"], doc_id))
            db.execute("UPDATE documents SET replaces_target=NULL WHERE id=?", (doc_id,))
            return "ready"
        row = db.execute(
            "UPDATE documents SET"
            "   status=CASE WHEN attempts < ? THEN 'pending' ELSE 'failed' END,"
            "   error=?, worker='', phase='',"
            "   finished_at=CASE WHEN attempts >= ? THEN ? ELSE finished_at END"
            " WHERE id=? AND status='processing' AND worker=? RETURNING id, status",
            (settings.max_attempts, err, settings.max_attempts, _now(), doc_id, worker)
        ).fetchone()
        return row["status"] if row else None


def holds_lease(doc_id: int, worker: str) -> bool:
    """租约检查：该行是否仍由本持有者持有（供长轮询期间随时自检）。"""
    with connect() as db:
        row = db.execute(
            "SELECT 1 FROM documents WHERE id=? AND status='processing' AND worker=?",
            (doc_id, worker)).fetchone()
    return row is not None


def reset_for_reparse(doc_id: int) -> bool:
    """重新解析：重置行（含处理中——旧执行者将由租约闭环自动止损）。"""
    with connect() as db:
        cur = db.execute(
            "UPDATE documents SET status='pending', worker='', attempts=0, error='',"
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
