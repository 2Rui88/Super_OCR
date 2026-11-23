"""知识库五件套工具（业务实现 + 注册表登记）—— 全只读。

引擎与注册表机制在内核；本模块只提供 ToolSpec 并在 setup() 时注册。
LLM 给的参数已经过 registry.ToolSpec.arguments 钳制。
"""
from __future__ import annotations

import asyncio
import json
import re
from html import escape
from html.parser import HTMLParser

from app import retrieval
from app.agent.registry import ToolSpec
from app.agent_business.citations import Citations
from app.db import connect

_BLOCK_MAX_CHARS = 400
_TABLE_MAX_CHARS = 2000

_CHUNKS = (
    "SELECT c.id, c.doc_id, c.page_num, c.bbox, c.type, c.row_index, c.heading_path,"
    " c.seek_text, c.html, c.crop_path, c.linear_text, c.vl_desc, d.filename"
    " FROM chunks c JOIN documents d ON d.id=c.doc_id"
    " WHERE d.status='ready' AND d.superseded_by IS NULL"
)


def _block(chunk: dict, citations: Citations) -> dict:
    _, _, text = chunk["seek_text"].partition("\n")
    text = text or chunk["seek_text"]
    if len(text) > _BLOCK_MAX_CHARS:
        text = text[:_BLOCK_MAX_CHARS] + "…可用 read_neighbors 查阅相邻块。"
    return {**citations.register(chunk), "text": text}


async def search_chunks(args: dict, citations: Citations) -> dict:
    return await _search(args, citations)


async def search_in_doc(args: dict, citations: Citations) -> dict:
    doc_id, error = await asyncio.to_thread(
        _resolve_document, args.get("doc_id"), args.get("doc_name", ""))
    if error:
        return {"message": error}
    return await _search(args, citations, doc_id)


def _resolve_document(doc_id: int | None, name: str) -> tuple[int | None, str]:
    if doc_id is None and not name:
        return None, "请提供制度文件名或文档 ID。"
    with connect() as db:
        base = "SELECT id, filename FROM documents WHERE status='ready' AND superseded_by IS NULL"
        if doc_id is not None:
            rows = db.execute(base + " AND id=?", (doc_id,)).fetchall()
        else:
            rows = db.execute(base + " AND filename=? COLLATE NOCASE LIMIT 2", (name,)).fetchall()
            if not rows:
                rows = db.execute(
                    base + " AND instr(lower(filename), lower(?))>0 ORDER BY id LIMIT 2",
                    (name,)).fetchall()
    if not rows:
        return None, "没有找到可查阅的现行文件。"
    if len(rows) > 1:
        return None, "文件名匹配到多份制度，请使用完整文件名或文档 ID。"
    return rows[0]["id"], ""


async def _search(args: dict, citations: Citations, doc_id: int | None = None) -> dict:
    if not args.get("query"):
        return {"count": 0, "blocks": [], "message": "请提供检索词。"}
    result = await retrieval.search(args.get("query", ""), doc_id=doc_id,
                                    top_k=args.get("top_k", 6))
    blocks = [_block(chunk, citations) for chunk in result["results"]]
    return {"count": len(blocks), "blocks": blocks}


def _get_chunk(db, args: dict):
    query = _CHUNKS + " AND c.id=?"
    if (scope := args.get("doc_id")) is not None:
        return db.execute(query + " AND c.doc_id=?", (args.get("chunk_id", 0), scope)).fetchone()
    return db.execute(query, (args.get("chunk_id", 0),)).fetchone()


class _TableRows(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self._row is not None and self._cell is not None:
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None
            self._cell = None


def _table_content(chunk) -> dict:
    parser = _TableRows()
    html = chunk["html"] or ""
    parser.feed(html)
    header = parser.rows[0] if parser.rows else []
    rows = parser.rows[1:]
    data = {"header": header, "rows": rows, "html": html}
    if not parser.rows:
        data["text"] = chunk["seek_text"].partition("\n")[2][:_BLOCK_MAX_CHARS]
    if len(json.dumps(data, ensure_ascii=False)) <= _TABLE_MAX_CHARS:
        return data

    edges = rows if len(rows) <= 2 else [rows[0], rows[-1]]
    width = 80
    while True:
        clipped = [[cell[:width] for cell in row[:12]] for row in [header, *edges]]
        preview_rows = []
        for index, row in enumerate(clipped):
            tag = "th" if index == 0 else "td"
            cells = "".join(f"<{tag}>{escape(cell)}</{tag}>" for cell in row)
            preview_rows.append(f"<tr>{cells}</tr>")
        preview = "<table>" + "".join(preview_rows) + "</table>"
        data = {"header": clipped[0], "rows": clipped[1:], "html": preview,
                "truncated": True, "omitted_rows": max(0, len(rows) - len(edges))}
        if len(json.dumps(data, ensure_ascii=False)) <= _TABLE_MAX_CHARS:
            return data
        width //= 2


def _read_table(args: dict):
    with connect() as db:
        chunk = _get_chunk(db, args)
        if chunk is None or chunk["type"] not in {"table_row", "table_full"}:
            return None
        if chunk["type"] == "table_full":
            return chunk
        return db.execute(
            _CHUNKS + " AND c.doc_id=? AND c.page_num=? AND c.bbox=?"
            " AND c.type='table_full' AND c.id<? ORDER BY c.id DESC LIMIT 1",
            (chunk["doc_id"], chunk["page_num"], chunk["bbox"], chunk["id"])).fetchone()


async def read_table(args: dict, citations: Citations) -> dict:
    table = await asyncio.to_thread(_read_table, args)
    if table is None:
        return {"message": "找不到对应的整表，请使用有效的表格块 ID。"}
    block = {**citations.register(table), **_table_content(table)}
    return {"count": 1, "blocks": [block]}


def _read_neighbors(args: dict):
    with connect() as db:
        chunk = _get_chunk(db, args)
        if chunk is None:
            return None
        doc_id, cid = chunk["doc_id"], chunk["id"]
        before = db.execute(
            _CHUNKS + " AND c.doc_id=? AND c.id<? ORDER BY c.id DESC LIMIT ?",
            (doc_id, cid, args.get("before", 1))).fetchall()
        after = db.execute(
            _CHUNKS + " AND c.doc_id=? AND c.id>? ORDER BY c.id LIMIT ?",
            (doc_id, cid, args.get("after", 1))).fetchall()
    return list(reversed(before)), after


async def read_neighbors(args: dict, citations: Citations) -> dict:
    neighbors = await asyncio.to_thread(_read_neighbors, args)
    if neighbors is None:
        return {"message": "找不到可查阅的资料块。"}
    before, after = neighbors
    blocks = [{**_block(chunk, citations), "position": position}
              for position, rows in (("before", before), ("after", after)) for chunk in rows]
    return {"count": len(blocks), "blocks": blocks}


async def read_figure(args: dict, citations: Citations) -> dict:
    figure = await asyncio.to_thread(_open_chunk, args)
    if figure is None or figure["type"] != "figure":
        return {"message": "找不到对应的流程图，请使用有效的流程图块 ID。"}
    block = {**citations.register(figure),
             "node_text": (figure["linear_text"]
                           or figure["seek_text"].partition("\n")[2])[:1500],
             "structure": (figure["vl_desc"] or "")[:1500]}
    if figure["crop_path"]:
        block["image_url"] = f"/api/v1/crops/{figure['id']}"
    return {"count": 1, "blocks": [block]}


def _open_chunk(args: dict):
    with connect() as db:
        return _get_chunk(db, args)


_ID = {"type": "integer", "minimum": 1, "maximum": (1 << 63) - 1}
_CHUNK_ARG = {"chunk_id": {**_ID, "description": "资料块 ID"}}
_QUERY = {
    "query": {"type": "string", "maxLength": 200, "description": "检索用的关键词或短问句"},
    "top_k": {"type": "integer", "minimum": 1, "maximum": retrieval.MAX_FINAL_K, "default": 6},
}
_NEIGHBORS = {"type": "integer", "minimum": 0, "maximum": 2, "default": 1}

SPECS = (
    ToolSpec("search_chunks", "检索现行制度", _QUERY, search_chunks,
             "跨文件搜索制度，返回带编号的资料块。"),
    ToolSpec("search_in_doc", "在指定的现行制度文件内检索",
             {**_QUERY, "doc_id": {**_ID, "description": "文档 ID"},
              "doc_name": {"type": "string", "maxLength": 255, "description": "文件名"}},
             search_in_doc, "指定文件名或 ID，在这份文件内查找。"),
    ToolSpec("read_table", "从表格行块读取其整表", _CHUNK_ARG, read_table,
             "查阅完整表格，核对表头、类别、金额和适用条件。"),
    ToolSpec("read_neighbors", "读取同一文档的相邻资料块",
             {**_CHUNK_ARG, "before": _NEIGHBORS, "after": _NEIGHBORS},
             read_neighbors, "查看当前块的前后文。"),
    ToolSpec("read_figure", "查阅流程图", _CHUNK_ARG, read_figure,
             "查看流程图节点、走向和裁图链接。"),
)
