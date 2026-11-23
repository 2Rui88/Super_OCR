"""FAISS 向量索引 —— IndexIDMap2 + IndexFlatIP，SQLite BLOB 是向量真身。

faiss_id 直接复用 chunk id（AUTOINCREMENT 不复用，删除/重放安全）。
索引文件是可再生缓存：损坏或换模型维度不符时，从 BLOB 全量重放（零 API 费）。
所有操作持进程内锁串行（单实例 asyncio 足够，无需文件锁）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
from pathlib import Path

import numpy as np

from app.config import settings
from app.db import connect

log = logging.getLogger("kb.vector")

_lock = asyncio.Lock()
_index = None  # 惰性加载的单例


def index_path() -> Path:
    return settings.data_dir / "faiss.index"


def meta_path() -> Path:
    return settings.data_dir / "faiss.meta.json"


def _load():
    global _index
    import faiss

    if _index is not None:
        return _index
    dim = settings.embed_dim
    if index_path().exists() and meta_path().exists():
        meta = json.loads(meta_path().read_text(encoding="utf-8"))
        if meta.get("dim") == dim and meta.get("model") == settings.embed_model:
            with open(index_path(), "rb") as fh:
                blob = np.fromfile(fh, dtype=np.uint8)
            _index = faiss.deserialize_index(blob)
            return _index
        log.warning("索引维度/模型不符（%s），自动重建", meta)
    _index = faiss.IndexIDMap2(faiss.IndexFlatIP(dim))
    return _index


def _persist_locked() -> None:
    """Python 字节流落盘：faiss C++ fopen 不认含中文的 UTF-8 路径（Windows）。"""
    import faiss

    tmp = index_path().with_suffix(".tmp")
    blob = faiss.serialize_index(_load())
    tmp.write_bytes(blob.tobytes())
    tmp.replace(index_path())  # 原子替换
    meta_path().write_text(json.dumps(
        {"dim": settings.embed_dim, "model": settings.embed_model,
         "updated_at": int(time.time())}, ensure_ascii=False), encoding="utf-8")


def vec_to_blob(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def blob_to_vec(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


async def add_chunks(doc_id: int) -> int:
    """A7 上架：读取本文档已写好 BLOB 的块，加入索引。返回入索引块数。"""
    async with _lock:
        def _do() -> int:
            with connect() as db:
                rows = db.execute(
                    "SELECT id, embedding FROM chunks"
                    " WHERE doc_id=? AND embedding IS NOT NULL AND faiss_id IS NULL",
                    (doc_id,)).fetchall()
            if not rows:
                return 0
            ids = np.array([r["id"] for r in rows], dtype=np.int64)
            mat = np.array([blob_to_vec(r["embedding"]) for r in rows],
                           dtype=np.float32)
            index = _load()
            index.add_with_ids(mat, ids)
            with connect() as db:
                db.executemany("UPDATE chunks SET faiss_id=? WHERE id=?",
                               [(int(i), int(i)) for i in ids])
            _persist_locked()
            return len(rows)
        return await asyncio.to_thread(_do)


async def remove_doc(doc_id: int) -> int:
    """索引同步：删除文档 → 按 chunk id 增量摘（不重建）。"""
    async with _lock:
        def _do() -> int:
            with connect() as db:
                ids = [r["id"] for r in db.execute(
                    "SELECT id FROM chunks WHERE doc_id=?", (doc_id,))]
            if not ids:
                return 0
            import faiss  # noqa: F401
            index = _load()
            removed = index.remove_ids(
                np.array(ids, dtype=np.int64))
            _persist_locked()
            return int(removed)
        return await asyncio.to_thread(_do)


async def rebuild() -> dict:
    """重建按钮：从 SQLite BLOB 重放（零 API 费，兼压实）。"""
    async with _lock:
        def _do() -> dict:
            with connect() as db:
                rows = db.execute(
                    "SELECT id, embedding FROM chunks"
                    " WHERE embedding IS NOT NULL").fetchall()
            global _index
            import faiss
            _index = faiss.IndexIDMap2(faiss.IndexFlatIP(settings.embed_dim))
            if rows:
                ids = np.array([r["id"] for r in rows], dtype=np.int64)
                mat = np.array([blob_to_vec(r["embedding"]) for r in rows],
                               dtype=np.float32)
                _index.add_with_ids(mat, ids)
                with connect() as db:
                    db.execute("UPDATE chunks SET faiss_id=NULL WHERE embedding IS NULL")
                    db.executemany("UPDATE chunks SET faiss_id=? WHERE id=?",
                                   [(int(i), int(i)) for i in ids])
            _persist_locked()
            return {"vectors": len(rows)}
        return await asyncio.to_thread(_do)


async def search(query_vec: list[float], top_k: int = 20) -> list[tuple[int, float]]:
    """向量检索：返回 [(chunk_id, cosine)]，余弦（向量已归一 + FlatIP）。"""
    def _do() -> list[tuple[int, float]]:
        index = _load()
        if index.ntotal == 0:
            return []
        vec = np.array([query_vec], dtype=np.float32)
        scores, ids = index.search(vec, min(top_k, index.ntotal))
        return [(int(i), float(s)) for i, s in zip(ids[0], scores[0]) if i != -1]
    return await asyncio.to_thread(_do)
