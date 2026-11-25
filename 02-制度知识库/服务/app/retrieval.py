"""检索层 —— 双路召回 + RRF 融合 + 拒答闸门（链路 B 的①②③④）。

- 向量路：FAISS Top20（余弦，向量已归一）；
- 关键词路：FTS5 trigram Top20；查询不足三字或 trigram 无命中时回退 LIKE
  （"印章""差旅"这类双字词 trigram 天然哑火，不回退会静默漏检）；
- 两路只召回就绪、未被替代的文档；
- 指定文档时，关键词和向量均在该文档内排序；
- RRF（K=60）融合，默认取 Top8；
- 拒答闸门：向量 Top1 余弦 < 阈值 且 关键词路无命中，双路皆弱才拒——
  关键词精确命中时不因向量分低误杀。
"""
from __future__ import annotations

import asyncio
import logging
import re

import numpy as np

from app import llm, vector
from app.config import settings
from app.db import connect

log = logging.getLogger("kb.retrieval")

TOP_K_PER_ROUTE = 20
RRF_K = 60
FINAL_K = 8
MAX_FINAL_K = 10
_FIGURE_APPENDIX = re.compile(r"附录\s*([a-z])(?![a-z])", re.I)
_FIGURE_TERMS = re.compile(r"流程图|泳道图|图注")
_TABLE_TERMS = re.compile(r"流程(?:图)?节点说明|节点说明表|工作指引")
_SEARCHABLE = (
    "d.status='ready'"
    " AND (c.type NOT IN ('table_full', 'table_row') OR NOT EXISTS ("
    " SELECT 1 FROM chunks f WHERE f.doc_id=c.doc_id AND f.page_num=c.page_num"
    " AND f.bbox=c.bbox AND f.type='figure' AND f.vl_desc LIKE '节点和岗位：%'))"
)

# 旧问答管线(改写点位/生成提示词/拒答闸门)已由 agent 引擎取代；
# 本模块只保留检索能力本体，供 agent_business 工具层调用。

def _fts_terms(query: str) -> list[str]:
    """把问句拆成可 OR 的检索词：中文连续段切 trigram 窗口，拉丁/数字段保留整词。

    整句当短语丢给 trigram 会要求原文含相同连续子串，必然漏检——
    必须拆词；bm25 负责排序，公共字窗口自然被压权。
    """
    terms: list[str] = []
    for run in re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]+", query):
        if run.isascii():
            if len(run) >= 3:
                terms.append(run)
        elif len(run) >= 3:
            terms.extend(run[i:i + 3] for i in range(len(run) - 2))
    return list(dict.fromkeys(terms))


def _cn_runs(query: str) -> list[str]:
    """中文连续段（≥2 字），LIKE 兜底用。"""
    return [r for r in re.findall(r"[\u4e00-\u9fff]{2,}", query)]


def _like_patterns(query: str, cap: int = 12) -> list[str]:
    """LIKE 兜底模式：中文段切 bigram 窗口（整段子串必然漏检，双字词才能各自命中），
    拉丁/数字段保留整词；上限 cap 防止 SQL 爆炸。"""
    pats: list[str] = []
    for run in _cn_runs(query):
        pats.extend(run[i:i + 2] for i in range(len(run) - 1))
    for run in re.findall(r"[A-Za-z0-9]{2,}", query):
        pats.append(run)
    return list(dict.fromkeys(pats))[:cap]


def _escape_like(p: str) -> str:
    return p.replace("%", chr(92) + "%").replace("_", chr(92) + "_")


def _keep_narrow(db, patterns: list[str], ratio: float = 0.30) -> list[str]:
    """df 过滤（两路统一口径）：LIKE 命中面超过 ratio 的词（"公司""标准"这类
    高频词）视为停用词丢弃——否则查无题的关键词路永远有命中，"双路皆弱才拒"
    永远不触发。"""
    total = db.execute(
        "SELECT COUNT(*) c FROM chunks c JOIN documents d ON d.id=c.doc_id"
        f" WHERE {_SEARCHABLE}").fetchone()["c"] or 1
    keep = []
    for p in patterns:
        n = db.execute(
            "SELECT COUNT(*) c FROM chunks c JOIN documents d ON d.id=c.doc_id"
            f" WHERE c.seek_text LIKE ? ESCAPE '\\' AND {_SEARCHABLE}",
            (f"%{_escape_like(p)}%",)).fetchone()["c"]
        if n / total <= ratio:
            keep.append(p)
    return keep


def _keyword_ids(query: str, doc_id: int | None = None) -> list[int]:
    """关键词路：trigram 词 OR 查询优先，双字词/无命中回退 LIKE。
    全库使用 df 过滤。文档内保留高频词，因为章节主题经常出现在该文档多数块。"""
    stripped = query.strip()
    scope = _SEARCHABLE
    params: tuple[int, ...] = ()
    if doc_id is not None:
        scope += " AND c.doc_id=?"
        params = (doc_id,)
    with connect() as db:
        terms = _fts_terms(stripped)
        if doc_id is None:
            terms = _keep_narrow(db, terms)
        if terms:
            match = " OR ".join(f'"{t}"' for t in terms)
            rows = db.execute(
                "SELECT c.id FROM chunks_fts f"
                " JOIN chunks c ON c.id = f.rowid"
                " JOIN documents d ON d.id = c.doc_id"
                f" WHERE chunks_fts MATCH ? AND {scope}"
                " ORDER BY rank, c.id LIMIT ?", (match, *params, TOP_K_PER_ROUTE)).fetchall()
            if rows:
                return [r["id"] for r in rows]
        # 回退：LIKE bigram 窗口 OR（双字词的救命路）
        pats = _like_patterns(stripped)
        if doc_id is None:
            pats = _keep_narrow(db, pats)
        if not pats:
            return []
        conds = " OR ".join("c.seek_text LIKE ? ESCAPE '\\'" for _ in pats)
        patterns = [f"%{_escape_like(p)}%" for p in pats]
        rows = db.execute(
            "SELECT c.id FROM chunks c JOIN documents d ON d.id=c.doc_id"
            f" WHERE ({conds}) AND {scope} ORDER BY c.id LIMIT ?",
            (*patterns, *params, TOP_K_PER_ROUTE)).fetchall()
        return [r["id"] for r in rows]


def _document_vector_hits(doc_id: int, query_vec: list[float]) -> list[tuple[int, float]]:
    """在目标文档的 SQLite 向量中计算余弦排序，无需经过全库候选。"""
    with connect() as db:
        rows = db.execute(
            "SELECT c.id, c.embedding FROM chunks c JOIN documents d ON d.id=c.doc_id"
            f" WHERE c.doc_id=? AND {_SEARCHABLE} AND length(c.embedding)=?",
            (doc_id, len(query_vec) * 4)).fetchall()
    if not rows:
        return []
    query = np.asarray(query_vec, dtype=np.float32)
    # llm.embed 已将数据库向量和查询向量都做过 L2 归一化。
    matrix = np.vstack([np.frombuffer(row["embedding"], dtype=np.float32) for row in rows])
    hits = [(row["id"], float(score)) for row, score in zip(rows, matrix @ query)]
    return sorted(hits, key=lambda hit: (-hit[1], hit[0]))[:TOP_K_PER_ROUTE]


def _searchable_chunk_ids() -> set[int]:
    with connect() as db:
        return {row["id"] for row in db.execute(
            "SELECT c.id FROM chunks c JOIN documents d ON d.id=c.doc_id"
            f" WHERE {_SEARCHABLE}")}


async def _global_vector_hits(query_vec: list[float]) -> list[tuple[int, float]]:
    eligible = await asyncio.to_thread(_searchable_chunk_ids)
    target = min(TOP_K_PER_ROUTE, len(eligible))
    if not target:
        return []
    limit = TOP_K_PER_ROUTE
    while True:
        candidates = await vector.search(query_vec, limit)
        hits = [hit for hit in candidates if hit[0] in eligible]
        if len(hits) >= target or len(candidates) < limit:
            return hits[:target]
        limit *= 2


def _appendix_figure_ids(question: str, doc_id: int | None) -> list[int]:
    """显式指向附录流程图时，用附录和已知文件定位原图块。"""
    appendices = {match.upper() for match in _FIGURE_APPENDIX.findall(question)}
    if len(appendices) != 1 or not _FIGURE_TERMS.search(question) or _TABLE_TERMS.search(question):
        return []
    if doc_id is None and "《" in question:
        from app.agent_business.document_scope import preferred_document

        doc_id = preferred_document(question)
        if doc_id is None:
            return []
    appendix = next(iter(appendices))
    with connect() as db:
        rows = db.execute(
            "SELECT c.id,c.doc_id,c.heading_path FROM chunks c JOIN documents d ON d.id=c.doc_id"
            f" WHERE {_SEARCHABLE} AND c.type='figure' AND c.vl_desc<>''"
            " AND (? IS NULL OR c.doc_id=?) ORDER BY c.page_num,c.id",
            (doc_id, doc_id),
        ).fetchall()
    matched = [row for row in rows if (head := _FIGURE_APPENDIX.match(row["heading_path"].lstrip()))
               and head.group(1).upper() == appendix]
    return [row["id"] for row in matched] if len({row["doc_id"] for row in matched}) == 1 else []


async def search(question: str, *, doc_id: int | None = None, top_k: int = FINAL_K) -> dict:
    """按范围检索，保留旧问答使用的拒答分数和结果字段。"""
    top_k = max(1, min(top_k, MAX_FINAL_K))
    # 向量路
    vector_hits: list[tuple[int, float]] = []
    if llm.available():
        try:
            vec = (await llm.embed([question]))[0]
            if doc_id is None:
                vector_hits = await _global_vector_hits(vec)
            else:
                vector_hits = await asyncio.to_thread(_document_vector_hits, doc_id, vec)
        except Exception:
            log.warning("向量检索失败，仅关键词路", exc_info=True)
    keyword_hits, appendix_hits = await asyncio.gather(
        asyncio.to_thread(_keyword_ids, question, doc_id),
        asyncio.to_thread(_appendix_figure_ids, question, doc_id),
    )

    # RRF 融合
    rrf: dict[int, float] = {}
    for rank, (cid, _score) in enumerate(vector_hits, 1):
        rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (RRF_K + rank)
    for rank, cid in enumerate(keyword_hits, 1):
        rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (RRF_K + rank)
    top_ids = list(dict.fromkeys([*appendix_hits, *sorted(rrf, key=rrf.get, reverse=True)]))

    # 拒答闸门：双路皆弱才拒
    vector_top1 = vector_hits[0][1] if vector_hits else 0.0
    refuse = vector_top1 < settings.reject_threshold and not (keyword_hits or appendix_hits)

    results = []
    if top_ids:
        marks = ",".join("?" * len(top_ids))
        with connect() as db:
            rows = db.execute(
                f"SELECT c.id, c.doc_id, c.page_num, c.bbox, c.type, c.row_index, c.heading_path,"
                f"       c.seek_text, c.html, c.crop_path, c.linear_text, c.vl_desc,"
                f"       c.source_mode, c.ocr_confidence,"
                f"       d.filename, d.sha256, d.pdf_path"
                f" FROM chunks c JOIN documents d ON d.id = c.doc_id"
                f" WHERE c.id IN ({marks}) AND {_SEARCHABLE}", tuple(top_ids)).fetchall()
            by_id = {r["id"]: dict(r) for r in rows}
        vector_ids = {cid for cid, _ in vector_hits}
        keyword_ids = set(keyword_hits)
        for cid in top_ids:
            if cid in by_id:
                item = by_id[cid]
                item["rrf"] = round(rrf.get(cid, 0.0), 6)
                item["routes"] = ("附录标题" if cid in appendix_hits else
                                  "向量+关键词" if cid in vector_ids and cid in keyword_ids
                                  else "向量" if cid in vector_ids else "关键词")
                results.append(item)
                if len(results) == top_k:
                    break
    return {"refuse": refuse, "vector_top1": round(vector_top1, 4),
            "keyword_hits": len(keyword_hits), "results": results}


def citation_label(chunk: dict) -> str:
    """出处文案：文件名 · 章节 · 第N页（面包屑缺失退化为仅页码）。"""
    label = chunk["filename"]
    if chunk.get("heading_path"):
        label += f" · {chunk['heading_path']}"
    label += f" · 第{chunk['page_num']}页"
    if chunk.get("type") == "table_row":
        label += f"（表格第{chunk.get('row_index', 0) + 1}行）" if chunk.get("row_index") is not None else ""
    elif chunk.get("type") == "figure":
        label += "（流程图）"
    return label
