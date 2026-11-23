"""任务库 —— SQLite 队列，API 与 worker 之间唯一的交接面。

纯基础设施：只认识任务状态机（queued/running/done/failed）和任务类型字符串，
不 import 任何 OCR 业务代码。并发约定：
- WAL + busy_timeout，多进程读写安全；
- 领取任务是一条原子 UPDATE（带文档全局互斥子查询），不需要额外锁；
- worker 心跳写在 workers 表，过期由任意存活 worker 收割（WORKER_LOST）。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 领取下一个任务的原子更新：
# 1) 只挑 queued；2) 文档类任务（document/document_text_layer/document_scan）全局并发
#    不超过 :max_documents（默认 1，重流水线防内存/CPU 争抢）；3) card 任务不受限；
# 4) card 优先于文档类（长 PDF 运行期间到达的证件最多等一份 PDF，而不是排在积压
#    文档队列之后），同级内按创建顺序 FIFO；5) :claimable_types 能力过滤
#    （当前 worker 全量装配，历史供 CPU 文字层角色使用，None = 不限）。
_CLAIM_SQL_TEMPLATE = """
UPDATE tasks
SET status='running', worker_id=:worker_id, attempts=attempts+1,
    started_at=:now, phase='', progress=0.0, pages_progress=NULL
WHERE id = (
    SELECT id FROM tasks
    WHERE status='queued'
      AND (task_type NOT IN ('document','document_text_layer','document_scan')
           OR (SELECT COUNT(*) FROM tasks
               WHERE task_type IN ('document','document_text_layer','document_scan')
                 AND status='running') < :max_documents){claim_filter}
    ORDER BY (task_type = 'card') DESC, created_at, id
    LIMIT 1
)
RETURNING id, task_type, filename, file_path, file_sha256, hint_type
"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type     TEXT NOT NULL,              -- card | document
    status        TEXT NOT NULL DEFAULT 'queued',  -- queued | running | done | failed
    phase         TEXT NOT NULL DEFAULT '',
    progress      REAL NOT NULL DEFAULT 0.0,
    pages_progress TEXT,                      -- 文档任务逐页进度 JSON：{total, pages:[{page,blocks,tables,duration_ms,warning}]}
    filename      TEXT NOT NULL,              -- 用户原始文件名（仅元数据，不用于磁盘路径）
    file_sha256   TEXT NOT NULL,
    file_path     TEXT NOT NULL,              -- 服务端生成的存储路径
    hint_type     TEXT,
    engine_type   TEXT,                       -- 领取时空缺，完成时写实际引擎
    result_json   TEXT,
    error_code    TEXT,
    error_message TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    worker_id     TEXT,
    created_at    TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_claim ON tasks(status, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_sha ON tasks(file_sha256);
CREATE INDEX IF NOT EXISTS idx_tasks_document_running ON tasks(task_type, status);

CREATE TABLE IF NOT EXISTS workers (
    worker_id              TEXT PRIMARY KEY,
    pid                    INTEGER,
    last_heartbeat_at      TEXT NOT NULL,
    card_model_loaded      INTEGER NOT NULL DEFAULT 0,
    document_model_loaded  INTEGER NOT NULL DEFAULT 0,
    current_task_id        INTEGER,
    status                 TEXT NOT NULL DEFAULT 'idle'  -- idle | busy
);

CREATE TABLE IF NOT EXISTS batches (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batch_items (
    batch_id INTEGER NOT NULL,
    task_id  INTEGER NOT NULL,
    PRIMARY KEY (batch_id, task_id)
);

CREATE TABLE IF NOT EXISTS eval_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    INTEGER NOT NULL,
    gt_sha256   TEXT NOT NULL,
    report_json TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cert_versions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    type_id      TEXT NOT NULL,
    yaml_text    TEXT NOT NULL,
    yaml_sha256  TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'draft',  -- draft | active | retired
    note         TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    activated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_cert_versions_type ON cert_versions(type_id, status, id);

CREATE TABLE IF NOT EXISTS cert_trials (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    type_id         TEXT NOT NULL,
    draft_version_id INTEGER NOT NULL,
    source          TEXT NOT NULL,
    draft_set_json  TEXT NOT NULL,   -- 试跑快照：{type_id: yaml_text}（draft 版替换对应类）
    ref_set_json    TEXT NOT NULL,   -- 参照快照：当时生效的整套
    draft_batch_id  INTEGER NOT NULL,
    ref_batch_id    INTEGER NOT NULL,
    gt_json         TEXT NOT NULL,   -- 标准答案快照（评测可复现）
    status          TEXT NOT NULL DEFAULT 'running', -- running|ready|confirmed|cancelled
    report_json     TEXT,
    created_at      TEXT NOT NULL,
    finished_at     TEXT
);

CREATE TABLE IF NOT EXISTS cert_state (
    id               INTEGER PRIMARY KEY CHECK (id = 1),
    active_set_hash  TEXT NOT NULL   -- 当前生效整套 YAML 的指纹，worker 心跳据此热重建
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


#: 重试上限与永久失败判定：attempts 到顶、上传即被拒、或转换确定性失败（损坏/
#: 加密/页数超限）的任务不再重试
MAX_TASK_ATTEMPTS = 3
PERMANENT_ERROR_CODES = {"UNSUPPORTED_FORMAT", "FILE_TOO_LARGE",
                         "CONVERT_FAILED", "DOC_TOO_LONG"}


@dataclass
class ClaimedTask:
    id: int
    task_type: str
    filename: str
    file_path: str
    file_sha256: str
    hint_type: str | None


class TaskStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(_SCHEMA)
            self._migrate(db)

    @staticmethod
    def _migrate(db) -> None:
        """旧库平滑加列：CREATE TABLE IF NOT EXISTS 不会给既有表补列。"""
        columns = {row[1] for row in db.execute("PRAGMA table_info(tasks)")}
        if "pages_progress" not in columns:
            db.execute("ALTER TABLE tasks ADD COLUMN pages_progress TEXT")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA busy_timeout=10000")
            with db:  # 事务范围：正常提交、异常回滚
                yield db
        finally:
            db.close()  # with sqlite3.Connection 只管事务不关连接，必须显式关

    # ------------------------------------------------------------ 任务写入/查询

    def create_task(self, *, task_type: str, filename: str, file_sha256: str,
                    file_path: str | Path, hint_type: str | None = None) -> int:
        with self._connect() as db:
            cursor = db.execute(
                "INSERT INTO tasks(task_type, filename, file_sha256, file_path,"
                " hint_type, created_at) VALUES (?,?,?,?,?,?)",
                (task_type, filename, file_sha256, str(file_path), hint_type, _now()),
            )
            return int(cursor.lastrowid)

    def create_failed_task(self, *, task_type: str, filename: str,
                           error_code: str, error_message: str) -> int:
        """上传即被拒的文件：没有落盘内容，直接落终态，重试接口按永久失败跳过。"""
        with self._connect() as db:
            cursor = db.execute(
                "INSERT INTO tasks(task_type, filename, file_sha256, file_path,"
                " status, error_code, error_message, created_at, finished_at)"
                " VALUES (?,?,?,?, 'failed', ?, ?, ?, ?)",
                (task_type, filename, "", "", error_code, error_message, _now(), _now()),
            )
            return int(cursor.lastrowid)

    def get_task(self, task_id: int) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def requeue_task(self, task_id: int) -> bool:
        """把 failed 任务重新排回队列（retry_failed 用，只对 failed 生效）。"""
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE tasks SET status='queued', error_code=NULL, error_message=NULL,"
                " worker_id=NULL, finished_at=NULL WHERE id=? AND status='failed'",
                (task_id,),
            )
            return cursor.rowcount == 1

    def requeue_running_task(self, task_id: int) -> bool:
        """领走但尚不具备执行条件的任务退回队列（试跑 setup 窗口用）。"""
        with self._connect() as db:
            cursor = db.execute("UPDATE tasks SET status='queued', worker_id=NULL"
                                " WHERE id=? AND status='running'", (task_id,))
            return cursor.rowcount == 1

    def fail_batch_open_tasks(self, batch_ids: list[int], *, error_code: str,
                              error_message: str) -> int:
        """把批次里未完成的任务直接置为失败（取消试跑时止损，不再空跑 GPU）。"""
        if not batch_ids:
            return 0
        placeholders = ",".join("?" for _ in batch_ids)
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE tasks SET status='failed', error_code=?, error_message=?,"
                " finished_at=? WHERE id IN"
                " (SELECT task_id FROM batch_items WHERE batch_id IN"
                f" ({placeholders}))"
                " AND status IN ('queued','running')",
                (error_code, error_message, _now(), *batch_ids))
            return cursor.rowcount

    # ------------------------------------------------------------------ 领取

    def claim_next_task(self, worker_id: str, max_running_documents: int = 1,
                        claimable_types: list[str] | None = None) -> ClaimedTask | None:
        """claimable_types=None 领取任意类型（兼容旧调用与既有测试）；
        否则只领取列表内的任务类型（worker 能力过滤）。"""
        if claimable_types:
            placeholders = ", ".join(f":t{i}" for i in range(len(claimable_types)))
            sql = _CLAIM_SQL_TEMPLATE.format(
                claim_filter=f" AND task_type IN ({placeholders})")
            params = {"worker_id": worker_id, "now": _now(),
                      "max_documents": max(1, max_running_documents)}
            params.update({f"t{i}": kind for i, kind in enumerate(claimable_types)})
        else:
            sql = _CLAIM_SQL_TEMPLATE.format(claim_filter="")
            params = {"worker_id": worker_id, "now": _now(),
                      "max_documents": max(1, max_running_documents)}
        with self._connect() as db:
            row = db.execute(sql, params).fetchone()
        if row is None:
            return None
        return ClaimedTask(**dict(row))

    def update_progress(self, task_id: int, phase: str, progress: float,
                        pages_progress: dict | None = None) -> None:
        """文档任务逐页上报：pages_progress 由 worker 在每页真实完成后整体写入
        （{total, pages:[…]}，内存累积 + 单条 UPDATE，无读改写竞态）。"""
        with self._connect() as db:
            if pages_progress is None:
                db.execute("UPDATE tasks SET phase=?, progress=? WHERE id=? AND status='running'",
                           (phase, max(0.0, min(1.0, progress)), task_id))
            else:
                db.execute("UPDATE tasks SET phase=?, progress=?, pages_progress=?"
                           " WHERE id=? AND status='running'",
                           (phase, max(0.0, min(1.0, progress)),
                            json.dumps(pages_progress, ensure_ascii=False), task_id))

    def finish_task(self, task_id: int, *, result_json: str, engine_type: str) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE tasks SET status='done', result_json=?, engine_type=?,"
                " phase='完成', progress=1.0, finished_at=? WHERE id=?",
                (result_json, engine_type, _now(), task_id),
            )

    def fail_task(self, task_id: int, *, error_code: str, error_message: str) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE tasks SET status='failed', error_code=?, error_message=?,"
                " finished_at=? WHERE id=?",
                (error_code, error_message, _now(), task_id),
            )

    # ------------------------------------------------------------- worker 管理

    def recover_worker_tasks(self, worker_id: str, *, error_code: str,
                             error_message: str) -> int:
        """worker 启动时认领自己上次没跑完的任务（API 重启不调用此方法）。"""
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE tasks SET status='failed', error_code=?, error_message=?,"
                " finished_at=? WHERE worker_id=? AND status='running'",
                (error_code, error_message, _now(), worker_id),
            )
            return cursor.rowcount

    def heartbeat(self, worker_id: str, *, pid: int, card_model_loaded: bool,
                  document_model_loaded: bool, current_task_id: int | None) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO workers(worker_id, pid, last_heartbeat_at, card_model_loaded,"
                " document_model_loaded, current_task_id, status)"
                " VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(worker_id) DO UPDATE SET pid=excluded.pid,"
                " last_heartbeat_at=excluded.last_heartbeat_at,"
                " card_model_loaded=excluded.card_model_loaded,"
                " document_model_loaded=excluded.document_model_loaded,"
                " current_task_id=excluded.current_task_id, status=excluded.status",
                (worker_id, pid, _now(), int(card_model_loaded), int(document_model_loaded),
                 current_task_id, "busy" if current_task_id else "idle"),
            )

    def reap_stale_workers(self, stale_seconds: float) -> list[str]:
        """心跳过期的 worker：其 running 任务标 WORKER_LOST，worker 行标 lost。"""
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)).isoformat(
            timespec="seconds")
        lost: list[str] = []
        with self._connect() as db:
            stale = db.execute(
                "SELECT worker_id FROM workers WHERE last_heartbeat_at < ? AND status != 'lost'",
                (cutoff,),
            ).fetchall()
            for row in stale:
                worker_id = row["worker_id"]
                db.execute(
                    "UPDATE tasks SET status='failed', error_code='WORKER_LOST',"
                    " error_message='worker 心跳过期，任务被回收', finished_at=?"
                    " WHERE worker_id=? AND status='running'",
                    (_now(), worker_id),
                )
                db.execute("UPDATE workers SET status='lost' WHERE worker_id=?", (worker_id,))
                lost.append(worker_id)
        return lost

    def list_workers(self, stale_seconds: float) -> list[dict]:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)).isoformat(
            timespec="seconds")
        with self._connect() as db:
            rows = db.execute("SELECT * FROM workers ORDER BY worker_id").fetchall()
        return [dict(row) | {"alive": row["status"] != "lost" and row["last_heartbeat_at"] >= cutoff}
                for row in rows]

    def healthcheck(self) -> bool:
        """SQLite 可写探针：写入并清理一张探测表。"""
        try:
            with self._connect() as db:
                db.execute("CREATE TABLE IF NOT EXISTS _health_probe (tick TEXT)")
                db.execute("DELETE FROM _health_probe")
                db.execute("INSERT INTO _health_probe VALUES (?)", (_now(),))
            return True
        except sqlite3.Error:
            return False

    # ---------------------------------------------------------------- 批次

    def list_batches(self, limit: int = 50) -> list[dict]:
        """批次列表（最近在前），逐批带状态计数概览（下拉切换历史批次用）。"""
        with self._connect() as db:
            rows = db.execute(
                "SELECT b.id, b.created_at,"
                " GROUP_CONCAT(DISTINCT t.task_type) AS types,"
                " COUNT(i.task_id) AS total,"
                " COALESCE(SUM(t.status='done'), 0) AS done,"
                " COALESCE(SUM(t.status='failed'), 0) AS failed,"
                " COALESCE(SUM(t.status IN ('queued','running')), 0) AS pending"
                " FROM batches b"
                " LEFT JOIN batch_items i ON i.batch_id = b.id"
                " LEFT JOIN tasks t ON t.id = i.task_id"
                " GROUP BY b.id ORDER BY b.id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def replace_batch_item(self, batch_id: int, old_task_id: int,
                           new_task_id: int) -> bool:
        """retype：批次项改指新任务（旧任务保留 failed 历史，批次行数不变）。"""
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE batch_items SET task_id=? WHERE batch_id=? AND task_id=?",
                (new_task_id, batch_id, old_task_id))
            return cursor.rowcount == 1

    def create_batch(self, task_ids: list[int]) -> int:
        with self._connect() as db:
            cursor = db.execute("INSERT INTO batches(created_at) VALUES (?)", (_now(),))
            batch_id = int(cursor.lastrowid)
            db.executemany("INSERT INTO batch_items(batch_id, task_id) VALUES (?,?)",
                           [(batch_id, task_id) for task_id in task_ids])
            return batch_id

    def batch_tasks(self, batch_id: int) -> list[dict]:
        """批次内任务完整行，按创建顺序。计数与状态由调用方聚合。"""
        with self._connect() as db:
            rows = db.execute(
                "SELECT t.* FROM batch_items b JOIN tasks t ON t.id = b.task_id"
                " WHERE b.batch_id=? ORDER BY t.id", (batch_id,)).fetchall()
        return [dict(row) for row in rows]

    def get_batch(self, batch_id: int) -> dict | None:
        """批次概览：逐文件状态 + 聚合计数 + 完成度（终态/总数）。"""
        with self._connect() as db:
            row = db.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            return None
        tasks = self.batch_tasks(batch_id)
        counts = {"queued": 0, "running": 0, "done": 0, "failed": 0}
        items = []
        for task in tasks:
            counts[task["status"]] += 1
            item = {"task_id": task["id"], "filename": task["filename"],
                    "status": task["status"], "attempts": task["attempts"],
                    "task_type": task["task_type"],
                    "phase": task["phase"], "progress": task["progress"],
                    "error_code": task["error_code"],
                    "error_message": task["error_message"],
                    "file_sha256": task["file_sha256"],
                    "created_at": task["created_at"],
                    "started_at": task["started_at"],
                    "finished_at": task["finished_at"]}
            if task["result_json"]:
                envelope = json.loads(task["result_json"])
                item["envelope"] = envelope
                item["kind"] = envelope["kind"]
                item["reason"] = envelope.get("reason", "")
                result = envelope["result"]
                item["doc_type"] = result.get("doc_type")
                item["doc_type_cn"] = result.get("doc_type_cn")
            items.append(item)
        total = len(items)
        return {
            "batch_id": batch_id,
            "created_at": row["created_at"],
            "total": total,
            "counts": counts,
            "progress": round((counts["done"] + counts["failed"]) / total, 4) if total else 1.0,
            "items": items,
        }

    def retry_failed(self, batch_id: int) -> dict:
        """只重排 failed 任务；attempts 到顶或上传被拒的按永久失败跳过。"""
        with self._connect() as db:
            rows = db.execute(
                "SELECT t.id, t.attempts, t.error_code FROM batch_items b"
                " JOIN tasks t ON t.id = b.task_id WHERE b.batch_id=?"
                " AND t.status='failed'", (batch_id,)).fetchall()
            eligible = [row["id"] for row in rows
                        if row["attempts"] < MAX_TASK_ATTEMPTS
                        and row["error_code"] not in PERMANENT_ERROR_CODES]
            skipped_permanent = len(rows) - len(eligible)
            if eligible:
                db.executemany(
                    "UPDATE tasks SET status='queued', error_code=NULL,"
                    " error_message=NULL, worker_id=NULL, finished_at=NULL"
                    " WHERE id=? AND status='failed'",
                    [(task_id,) for task_id in eligible])
        return {"requeued": len(eligible), "skipped_permanent": skipped_permanent}

    # ---------------------------------------------------------------- 评测

    def save_eval_run(self, batch_id: int, gt_sha256: str, report_json: str) -> int:
        with self._connect() as db:
            cursor = db.execute(
                "INSERT INTO eval_runs(batch_id, gt_sha256, report_json, created_at)"
                " VALUES (?,?,?,?)", (batch_id, gt_sha256, report_json, _now()))
            return int(cursor.lastrowid)

    # ---------------------------------------------------------------- 证件配置版本

    def insert_cert_version(self, *, type_id: str, yaml_text: str, yaml_sha256: str,
                            status: str, note: str = "") -> int:
        with self._connect() as db:
            if status == "draft":  # 同类只保留一个当前草稿：旧草稿转 retired
                db.execute("UPDATE cert_versions SET status='retired'"
                           " WHERE type_id=? AND status='draft'", (type_id,))
            cursor = db.execute(
                "INSERT INTO cert_versions(type_id, yaml_text, yaml_sha256, status,"
                " note, created_at, activated_at) VALUES (?,?,?,?,?,?,?)",
                (type_id, yaml_text, yaml_sha256, status, note, _now(),
                 _now() if status == "active" else None))
            return int(cursor.lastrowid)

    def list_cert_versions(self, type_id: str | None = None,
                           status: str | None = None) -> list[dict]:
        sql = "SELECT * FROM cert_versions"
        conds, params = [], []
        if type_id is not None:
            conds.append("type_id=?"); params.append(type_id)
        if status is not None:
            conds.append("status=?"); params.append(status)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY id DESC"
        with self._connect() as db:
            rows = db.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def promote_cert_version(self, version_id: int) -> dict | None:
        """draft → active（原子切换）：该类旧 active 转 retired。返回版本行。"""
        with self._connect() as db:
            row = db.execute("SELECT * FROM cert_versions WHERE id=?",
                             (version_id,)).fetchone()
            if row is None:
                return None
            db.execute("UPDATE cert_versions SET status='retired', activated_at=NULL"
                       " WHERE type_id=? AND status='active'", (row["type_id"],))
            db.execute("UPDATE cert_versions SET status='active', activated_at=?"
                       " WHERE id=?", (_now(), version_id))
        return dict(row)

    def retire_cert_version(self, version_id: int) -> bool:
        with self._connect() as db:
            cursor = db.execute("UPDATE cert_versions SET status='retired'"
                                " WHERE id=? AND status='draft'", (version_id,))
            return cursor.rowcount == 1

    def active_cert_texts(self) -> dict[str, str]:
        """当前生效整套：{type_id: yaml_text}。"""
        with self._connect() as db:
            rows = db.execute("SELECT type_id, yaml_text FROM cert_versions"
                              " WHERE status='active' ORDER BY type_id").fetchall()
        return {row["type_id"]: row["yaml_text"] for row in rows}

    def get_cert_state_hash(self) -> str | None:
        with self._connect() as db:
            row = db.execute("SELECT active_set_hash FROM cert_state WHERE id=1").fetchone()
        return row["active_set_hash"] if row else None

    def set_cert_state_hash(self, active_set_hash: str) -> None:
        with self._connect() as db:
            db.execute("INSERT INTO cert_state(id, active_set_hash) VALUES (1, ?)"
                       " ON CONFLICT(id) DO UPDATE SET active_set_hash=excluded.active_set_hash",
                       (active_set_hash,))

    # ---------------------------------------------------------------- 证件试跑

    def insert_cert_trial(self, **fields) -> int:
        columns = ",".join(fields)
        placeholders = ",".join("?" for _ in fields)
        with self._connect() as db:
            cursor = db.execute(
                f"INSERT INTO cert_trials({columns}, created_at)"
                f" VALUES ({placeholders}, ?)",
                (*fields.values(), _now()))
            return int(cursor.lastrowid)

    def update_cert_trial(self, trial_id: int, **fields) -> None:
        assignments = ",".join(f"{key}=?" for key in fields)
        with self._connect() as db:
            db.execute(f"UPDATE cert_trials SET {assignments} WHERE id=?",
                       (*fields.values(), trial_id))

    def get_cert_trial(self, trial_id: int) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM cert_trials WHERE id=?",
                             (trial_id,)).fetchone()
        return dict(row) if row else None

    def list_cert_trials(self, limit: int = 50) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM cert_trials ORDER BY id DESC LIMIT ?",
                              (limit,)).fetchall()
        return [dict(row) for row in rows]

    def cert_trial_by_task(self, task_id: int) -> dict | None:
        """试跑任务反查归属（worker 领到 cert_trial_card 任务时定位快照与角色）。"""
        with self._connect() as db:
            row = db.execute(
                "SELECT tr.*, b.id AS batch_id,"
                " CASE WHEN b.id = tr.draft_batch_id THEN 'draft' ELSE 'ref' END AS role"
                " FROM batch_items bi"
                " JOIN batches b ON b.id = bi.batch_id"
                " JOIN cert_trials tr ON b.id IN (tr.draft_batch_id, tr.ref_batch_id)"
                " WHERE bi.task_id=? LIMIT 1", (task_id,)).fetchone()
        return dict(row) if row else None
