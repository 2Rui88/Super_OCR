"""文档资源 —— 导入（A1 收件和入队）、文档库列表、解析详情、重新解析、删除。"""
from __future__ import annotations

import hashlib
import sqlite3

from fastapi import APIRouter, File, HTTPException, UploadFile

from app import queue, vector
from app.config import settings
from app.db import connect
from app.pipeline import store_upload

router = APIRouter(tags=["documents"])

_STATUS_CN = {"pending": "待处理", "processing": "处理中", "ready": "就绪", "failed": "失败"}


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "filename": row["filename"], "fmt": row["fmt"],
        "status": row["status"], "status_cn": _STATUS_CN.get(row["status"], row["status"]),
        "phase": row["phase"], "error": row["error"], "attempts": row["attempts"],
        "page_count": row["page_count"], "table_count": row["table_count"],
        "figure_count": row["figure_count"],
        "created_at": row["created_at"], "finished_at": row["finished_at"],
    }


@router.post("/documents")
async def import_documents(files: list[UploadFile] = File(...)):
    """批量导入：每个上传文件独立入队，异步处理。"""
    results = []
    for f in files:
        filename = f.filename or "unnamed"
        ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext not in settings.allowed_exts:
            results.append({"filename": filename, "ok": False,
                            "error": f"不支持的格式 {ext or '(无扩展名)'}"})
            continue
        data = await f.read()
        if len(data) > settings.max_upload_mb * 1024 * 1024:
            results.append({"filename": filename, "ok": False, "error": "超过大小上限"})
            continue
        sha = hashlib.sha256(data).hexdigest()
        path = store_upload(data, sha, ext)
        outcome, doc_id = queue.enqueue(
            filename, ext, sha, pdf_path=str(path) if ext == ".pdf" else None,
        )
        results.append({"filename": filename, "ok": True, "outcome": outcome, "doc_id": doc_id})
    return {"results": results}


@router.get("/documents")
def list_documents():
    """文档库列表 + 统计卡数据（文件/页/表格/流程图）。"""
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM documents ORDER BY id DESC").fetchall()
        return {
            "stats": {
                "files": len(rows),
                "pages": sum(r["page_count"] or 0 for r in rows),
                "tables": sum(r["table_count"] or 0 for r in rows),
                "figures": sum(r["figure_count"] or 0 for r in rows),
                "pending": sum(1 for r in rows if r["status"] == "pending"),
                "processing": sum(1 for r in rows if r["status"] == "processing"),
                "failed": sum(1 for r in rows if r["status"] == "failed"),
            },
            "documents": [_row_to_dict(r) for r in rows],
        }


@router.get("/documents/{doc_id}")
def document_detail(doc_id: int):
    """解析详情：目录结构（TITLE/面包屑聚合）+ 块列表。"""
    with connect() as db:
        row = db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "文档不存在")
        chunks = db.execute(
            "SELECT id, page_num, bbox, type, row_index, heading_path, seek_text,"
            " html, crop_path, linear_text, vl_desc, source_mode, ocr_confidence"
            " FROM chunks WHERE doc_id=?"
            " ORDER BY id", (doc_id,)).fetchall()
    toc: list[dict] = []
    seen = set()
    for c in chunks:
        h = c["heading_path"]
        if h and h not in seen:
            seen.add(h)
            toc.append({"heading": h, "page_num": c["page_num"]})
    return {
        "document": _row_to_dict(row),
        "toc": toc,
        "chunks": [dict(c) for c in chunks],
    }


@router.post("/documents/{doc_id}/reparse")
def reparse(doc_id: int):
    """重新解析：重置行重投。处理中的文档也可重置——旧执行者由租约闭环自动止损；
    就绪文档不重烧 OCR，直接从存档 JSON 重放 A4。"""
    if not queue.reset_for_reparse(doc_id):
        raise HTTPException(404, "文档不存在")
    return {"ok": True, "doc_id": doc_id}


@router.delete("/documents/{doc_id}")
async def delete_document(doc_id: int):
    """删除：清块（FTS 随触发器同步）+ FAISS 增量摘 + 删行。"""
    with connect() as db:
        exists = db.execute("SELECT 1 FROM documents WHERE id=?", (doc_id,)).fetchone()
    if exists is None:
        raise HTTPException(404, "文档不存在")
    await vector.remove_doc(doc_id)   # 先摘索引再删行（chunk id 需还在）
    with connect() as db:
        db.execute("DELETE FROM documents WHERE id=?", (doc_id,))
        db.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
    return {"ok": True}


@router.post("/index/rebuild")
async def rebuild_index():
    """重建按钮：从 SQLite BLOB 重放 FAISS（零 API 费，兼压实）。"""
    return await vector.rebuild()
