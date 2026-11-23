"""数据库 —— SQLite WAL 连接与建表。

约定（与项目一 task_store 一致）：每操作独立连接，用完必关；
WAL + busy_timeout，多进程/多协程读写安全。
documents 行兼任摄入队列（心跳租约），chunks 是检索与出处的一等公民。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    filename       TEXT NOT NULL,               -- 用户原始文件名（仅元数据）
    fmt            TEXT NOT NULL,               -- doc | docx | pdf
    sha256         TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending',  -- pending|processing|ready|failed
    phase          TEXT NOT NULL DEFAULT '',     -- 处理阶段描述（转格式/识别中 第n/N页/分块…）
    worker         TEXT NOT NULL DEFAULT '',     -- 租约持有者 主机名:pid
    lease_token    TEXT,                         -- 每次领单生成，仅该次执行可写
    heartbeat_at   INTEGER NOT NULL DEFAULT 0,   -- unixepoch 秒；领单即第 0 跳
    attempts       INTEGER NOT NULL DEFAULT 0,
    error          TEXT NOT NULL DEFAULT '',
    page_count     INTEGER, table_count INTEGER, figure_count INTEGER,
    superseded_by  INTEGER,                      -- 旧版指向新版 id（检索默认过滤）
    replaces_target INTEGER,                     -- 导入时声明的"替代旧版本"目标
    pdf_path       TEXT,                         -- 实际送识别的 PDF（原件或转换产物）
    p1_request_key TEXT,                         -- POST 前持久化，响应丢失时用同一键重送
    p1_task_id     INTEGER,                      -- 已收到的 OCR 任务编号
    p1_result_json TEXT,                         -- 项目一返回整包 = 事实源
    created_at     INTEGER NOT NULL,
    finished_at    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_documents_sha ON documents(sha256);
CREATE INDEX IF NOT EXISTS idx_documents_claim ON documents(status, id);

CREATE TABLE IF NOT EXISTS chunks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id         INTEGER NOT NULL,
    page_num       INTEGER NOT NULL,
    bbox           TEXT NOT NULL DEFAULT '[0,0,0,0]',   -- 归一化 [x0,y0,x1,y1] JSON
    type           TEXT NOT NULL,               -- text|table_row|table_full|figure
    row_index      INTEGER,                     -- table_row：命中行高亮用
    heading_path   TEXT NOT NULL DEFAULT '',    -- 章节面包屑（最近 TITLE）
    seek_text      TEXT NOT NULL,               -- 检索文本 = 文件名 + 块内容
    html           TEXT,                        -- 表格 HTML 全程保留
    crop_path      TEXT,                        -- 流程图裁图（阶段三）
    linear_text    TEXT,                        -- 流程图节点文字（坐标序）
    vl_desc        TEXT,                        -- 视觉模型结构描述（阶段三）
    source_mode    TEXT,                        -- text_layer | ocr
    ocr_confidence REAL,                        -- OCR 块置信度；直读视为 1.0
    embedding      BLOB,                        -- 向量真身（阶段三写入）
    faiss_id       INTEGER,
    created_at     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);

-- 关键词检索路：FTS5 trigram（外部内容表，触发器同步）。
-- 查询不足三字（如"印章""差旅"）由检索层回退 LIKE，trigram 对其天然哑火。
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    seek_text, content='chunks', content_rowid='id', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS chunks_fts_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, seek_text) VALUES (new.id, new.seek_text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_fts_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, seek_text)
    VALUES ('delete', old.id, old.seek_text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_fts_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, seek_text)
    VALUES ('delete', old.id, old.seek_text);
    INSERT INTO chunks_fts(rowid, seek_text) VALUES (new.id, new.seek_text);
END;

CREATE TABLE IF NOT EXISTS conversations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL,
    role            TEXT NOT NULL,               -- user | assistant | tool
    content         TEXT NOT NULL,
    citations_json  TEXT,                        -- [{chunk_id,label}]
    created_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id);

CREATE TABLE IF NOT EXISTS eval_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    category     TEXT NOT NULL,                  -- 正文|表格|流程图|查无|新旧版|多轮
    question     TEXT NOT NULL,
    gold_answer  TEXT NOT NULL DEFAULT '',
    gold_files   TEXT NOT NULL DEFAULT '[]',     -- JSON 数组：应引用文件名
    created_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS eval_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at     INTEGER NOT NULL,
    finished_at    INTEGER,
    config_json    TEXT NOT NULL DEFAULT '{}',   -- 当次检索配置快照
    summary_json   TEXT
);

CREATE TABLE IF NOT EXISTS eval_results (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL,
    item_id      INTEGER NOT NULL,
    answer       TEXT NOT NULL DEFAULT '',
    ok           INTEGER,                        -- 答案判对 1/0/NULL(未跑)
    citation_ok  INTEGER,                        -- 出处命中金标 1/0
    refused      INTEGER,                        -- 查无题是否正确拒答
    attribution  TEXT,                           -- 归因：ocr|retrieval|generation|''
    detail_json  TEXT,
    duration_ms  INTEGER
);
"""


def init_db() -> None:
    # WAL 是库级持久属性，初始化设一次即可（connect 不再逐连接重复设置）
    with connect() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(_SCHEMA)
        db.execute("BEGIN IMMEDIATE")
        columns = {row["name"] for row in db.execute("PRAGMA table_info(messages)")}
        for column in ("tool_calls", "tool_call_id"):
            if column not in columns:
                db.execute(f"ALTER TABLE messages ADD COLUMN {column} TEXT")
        columns = {row["name"] for row in db.execute("PRAGMA table_info(documents)")}
        for column, sql_type in (("lease_token", "TEXT"), ("p1_request_key", "TEXT"),
                                 ("p1_task_id", "INTEGER")):
            if column not in columns:
                db.execute(f"ALTER TABLE documents ADD COLUMN {column} {sql_type}")


@contextmanager
def connect(db_path: Path | None = None):
    db = sqlite3.connect(db_path or settings.db_path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=10000")
    try:
        with db:
            yield db
    finally:
        db.close()
