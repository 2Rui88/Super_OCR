"""引用注册表（业务）—— 本轮 chunk_id → [n] 单调编号，终局导出。"""
from __future__ import annotations

from app.retrieval import citation_label


class Citations:
    def __init__(self) -> None:
        self._items: dict[int, dict] = {}

    def register(self, chunk) -> dict:
        chunk_id = chunk["id"]
        if chunk_id not in self._items:
            self._items[chunk_id] = {
                "n": len(self._items) + 1, "chunk_id": chunk_id,
                "label": citation_label(dict(chunk)), "type": chunk["type"],
            }
        return dict(self._items[chunk_id])

    def export(self, chunk_ids: set[int]) -> list[dict]:
        """只导出终答证据仍可见的引用（与上下文预算裁剪保持一致）。"""
        return [dict(item) for cid, item in self._items.items() if cid in chunk_ids]
