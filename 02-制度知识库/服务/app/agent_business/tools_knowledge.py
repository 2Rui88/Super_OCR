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
from pathlib import Path

from app import figures, retrieval
from app.agent.registry import ToolSpec
from app.agent_business.articles import label_articles, last_article, select_clause, without_heading
from app.agent_business.citations import Citations
from app.agent_business.document_scope import (
    preferred_document, requested_document, resolve_document as _resolve_document,
)
from app.agent_business.policy_calculations import PAGE_QUESTION, REPORT_HEADING
from app.agent_business.policy_editions import current_editions
from app.db import connect

_BLOCK_MAX_CHARS = 400
_FIGURE_MAX_CHARS = 2300
_TABLE_MAX_CHARS = 2000
_FLOW_QUESTION = re.compile(r"流程|节点|环节|图注")
_FLOW_ORDER = re.compile(r"之后|接下来|下一步|依次|怎么走|走向|流向|才结束|后续|流程图|图注")
_TABLE_ONLY = re.compile(r"节点说明|工作指引|说明表|表格")
_APPENDIX = re.compile(r"附录\s*([A-Za-z])", re.IGNORECASE)
_NODE = re.compile(r"(?<!\d)\d{3}(?!\d)")
_TABLE_NODE = re.compile(r":\s*(\d{3})\s*\|")
_QUOTED_TITLE = re.compile(r"《([^》]+)》")
_SYMBOLS = re.compile(r"[△○□◎●◇◆]")
_STEP_QUESTION = re.compile(r"(?:步骤|节点)\s*(\d{1,4})")
_DESTINATION = re.compile(r"(?:去|到|赴)([\u4e00-\u9fff]{2,10}?)(?:市)?出差")
_DOC_NUMBER = re.compile(r"([\u4e00-\u9fff]{2,12})[〔（(\[【](\d{4})[〕）)\]】](\d+)号")
_TOC_LINE = re.compile(r"^\s*(\d+(?:\.\d+)+)\s*(.+?)\s*[.…]{3,}\s*(\d{1,4})\s*$")

_CHUNKS = (
    "SELECT c.id, c.doc_id, c.page_num, c.bbox, c.type, c.row_index, c.heading_path,"
    " c.seek_text, c.html, c.crop_path, c.linear_text, c.vl_desc, d.filename, d.sha256, d.pdf_path"
    " FROM chunks c JOIN documents d ON d.id=c.doc_id"
    " WHERE d.status='ready'"
)


def _native_lines(chunk) -> tuple[list[str], str]:
    path = chunk.get("pdf_path")
    if not path or not Path(path).is_file():
        return [], ""
    import pymupdf

    try:
        with pymupdf.open(path) as pdf:
            page = pdf[chunk["page_num"] - 1]
            footer = [block[4].strip() for block in page.get_text("blocks")
                      if block[1] >= page.rect.height * 0.9 and re.fullmatch(r"\d{1,4}", block[4].strip())]
            return page.get_text().splitlines(), footer[0] if len(footer) == 1 else ""
    except (OSError, IndexError, RuntimeError, ValueError):
        return [], ""


def _block(chunk: dict, citations: Citations, query: str = "", text_override: str | None = None) -> dict:
    chunk = dict(chunk)
    if chunk["type"] == "figure" and chunk["vl_desc"]:
        structure = figures.verified_description(chunk) if chunk.get("sha256") else ""
        return {**citations.register(chunk), "structure": (structure or chunk["vl_desc"])[:_FIGURE_MAX_CHARS]}
    _, _, text = chunk["seek_text"].partition("\n")
    text = text_override if text_override is not None else (text or chunk["seek_text"])
    question = citations.question or query
    terms = retrieval._fts_terms(question)
    article = ""
    footer = ""
    lines = []
    diagram = None
    contents = {}
    if chunk["type"] == "text":
        if ("图示说明" in text and _SYMBOLS.search(text)) or PAGE_QUESTION.search(question):
            lines, footer = _native_lines(chunk)
            if chunk["heading_path"] == "目录":
                for line in lines or text.splitlines():
                    entry = _TOC_LINE.fullmatch(line)
                    if entry and entry[2] in question:
                        contents[entry[1]] = entry[3]
            for line in lines:
                if "图示说明" in line and len(_SYMBOLS.findall(line)) > len(_SYMBOLS.findall(text)):
                    text = "\n".join(line.strip() if "图示说明" in part else part for part in text.splitlines())
                    break
            if step := _STEP_QUESTION.search(question):
                legend = next((line for line in lines if "图示说明" in line and _SYMBOLS.search(line)), "")
                positions = [index for index, line in enumerate(lines[:-1]) if line.strip() == step[1]]
                if legend and len(positions) == 1:
                    action = lines[positions[0] + 1].strip()
                    if re.fullmatch(r"[\u4e00-\u9fff]{2,30}", action):
                        diagram = {"legend": legend, "step": step[1], "action": action}
        text = label_articles(chunk, without_heading(text, chunk["heading_path"]))
        city = _DESTINATION.search(question)
        if city and city.group(1) in text and "城市类别" in text:
            text, article = select_clause(text, [city.group(1), "城市类别"])
        else:
            text, article = select_clause(text, terms)
    elif chunk["type"] in ("table_row", "table_full"):
        matched_row = _named_table_row(chunk, question) if text_override is None else []
        if matched_row:
            text = " | ".join(matched_row)
            if "文号" in question and (number := _DOC_NUMBER.search(re.sub(r"\s+", "", text))):
                native, _ = _native_lines(chunk)
                verified = next((match[0] for match in _DOC_NUMBER.finditer(re.sub(r"\s+", "", "".join(native)))
                                 if match.groups() == number.groups()), "")
                if verified:
                    text = " | ".join(verified if number[0] in re.sub(r"\s+", "", cell) else cell
                                      for cell in matched_row)
    if len(text) > _BLOCK_MAX_CHARS:
        if terms:
            paragraphs = text.splitlines()
            scores = [sum(term.casefold() in part.casefold() for term in terms) for part in paragraphs]
            relevant = scores.index(max(scores))
            if (relevant and scores[relevant] > 0
                    and len("\n".join(paragraphs[:relevant + 1])) > _BLOCK_MAX_CHARS):
                text = "\n".join(paragraphs[relevant:])
        if len(text) > _BLOCK_MAX_CHARS:
            text = text[:_BLOCK_MAX_CHARS] + "…可用 read_neighbors 查阅相邻块。"
    if footer and str(chunk["page_num"]) != footer and PAGE_QUESTION.search(question):
        text = f"本段在 PDF 第{chunk['page_num']}页，正文印刷页码为 {footer}。\n{text}"
    block = {**citations.register(chunk, article), "text": text}
    if lines and PAGE_QUESTION.search(question) and chunk["heading_path"]:
        heading = re.sub(r"\s+", "", chunk["heading_path"])
        block["location"] = {
            "pdf_page": chunk["page_num"], "printed_page": footer,
            "heading": chunk["heading_path"], "source_format": Path(chunk["filename"]).suffix.lower(),
            "starts_section": any(re.sub(r"\s+", "", line) == heading for line in lines),
        }
    if diagram:
        block["diagram"] = diagram
    if contents:
        block["contents"] = contents
    if chunk["type"] == "table_full" and ("设计变更" in question or "住宿费" in question):
        rows = _html_rows(chunk)
        if rows and len(rows) <= 12 and len(rows[0]) <= 10:
            block["policy_table"] = rows
    return block


async def search_chunks(args: dict, citations: Citations) -> dict:
    doc_id, error = await asyncio.to_thread(requested_document, citations.question)
    if error:
        return {"message": error}
    if doc_id is not None:
        return await _search(args, citations, doc_id)
    preferred = await asyncio.to_thread(preferred_document, citations.question)
    if preferred is not None:
        result = await _search(args, citations, preferred)
        if result["count"]:
            return result
    return await _search(args, citations, consider_editions=True)


async def search_in_doc(args: dict, citations: Citations) -> dict:
    doc_id, error = await asyncio.to_thread(
        _resolve_document, args.get("doc_id"), args.get("doc_name", ""), citations.question)
    if error:
        return {"message": error}
    return await _search(args, citations, doc_id)


def _literal_hits(question: str, doc_id: int | None) -> list[dict]:
    appendix = _APPENDIX.search(question)
    nodes = set(_NODE.findall(question)) if appendix and "节点" in question else set()
    titles = [re.sub(r"\s+", "", m.group(1)) for m in _QUOTED_TITLE.finditer(question)]
    titles = [title for title in titles if len(title) >= 5]
    if not (nodes or titles):
        return []
    scope = " AND c.doc_id=?" if doc_id is not None else ""
    params = (doc_id,) if doc_id is not None else ()
    results = []
    with connect() as db:
        if nodes:
            rows = db.execute(
                _CHUNKS + scope + " AND c.type='table_row' AND c.heading_path LIKE ? ORDER BY c.id",
                (*params, f"附录{appendix.group(1)}%"),
            )
            for row in rows:
                field = _TABLE_NODE.search(row["seek_text"].partition("\n")[2])
                if field and field.group(1) in nodes:
                    results.append(dict(row))
        elif doc_id is None:
            for title in titles:
                rows = db.execute(
                    _CHUNKS + " AND c.type IN ('table_row','text')"
                    " AND (instr(replace(c.seek_text, ' ', ''), ?) > 0"
                    " OR instr(replace(c.heading_path, ' ', ''), ?) > 0)"
                    " ORDER BY c.type='table_row' DESC, c.id LIMIT 8",
                    (title, title),
                )
                for row in rows:
                    body = re.sub(r"\s+", "", row["seek_text"].partition("\n")[2])
                    if title in body or title in re.sub(r"\s+", "", row["heading_path"]):
                        results.append(dict(row))
    return results


def _signing_clauses(hits: list[dict], question: str) -> list[tuple[dict, str]]:
    docs = list(dict.fromkeys(hit["doc_id"] for hit in hits))[:2]
    if not docs:
        return []
    marks = ",".join("?" for _ in docs)
    with connect() as db:
        rows = db.execute(
            _CHUNKS + f" AND c.doc_id IN ({marks}) AND c.type IN ('text','table_row')"
            " AND (instr(c.seek_text,'签署批准') OR "
            "(c.type='table_row' AND instr(c.seek_text, '2: 批准 |') AND c.heading_path LIKE '附录%'))"
            " ORDER BY c.id", docs,
        ).fetchall()
    terms = retrieval._fts_terms(question)
    ranked = []
    for raw in rows:
        chunk = dict(raw)
        text = chunk["seek_text"].partition("\n")[2]
        if "签署批准" not in text:
            node = _TABLE_NODE.search(text)
            if not node:
                continue
            lines, _ = _native_lines(chunk)
            if node.group(1) not in lines:
                continue
            start = lines.index(node.group(1))
            native = "".join(lines[start + 2:start + 10])
            clause = re.search(r"[^—。]*签署批准[^—。]*。", native)
            if not clause:
                continue
            text = f"节点 {node.group(1)} 批准。{clause.group()}"
        ranked.append((sum(term in text for term in terms), chunk, text))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [(chunk, text) for score, chunk, text in ranked[:2] if score]


def _text_continuations(hits: list[dict]) -> dict:
    candidates = [hit for hit in hits if hit["type"] == "text" and hit.get("heading_path")
                  and hit.get("bbox") and json.loads(hit["bbox"])[3] >= 0.88]
    if not candidates:
        return {}
    pairs = {}
    with connect() as db:
        for hit in candidates:
            chunk = _get_chunk(db, {"chunk_id": hit["id"]})
            if not chunk or json.loads(chunk["bbox"])[3] < 0.88:
                continue
            page = chunk["page_num"]
            text = chunk["seek_text"].partition("\n")[2].rstrip()
            # 只在页脚的纯数字与 PDF 实页码一致、下页文本续接完整金额时移除。
            suffix = re.search(rf"(?<!\d)\s*{page}$", text)
            if not suffix:
                continue
            clean = text[:suffix.start()].rstrip()
            if not clean or clean.endswith(("。", "！", "？", "；")):
                continue
            lines, footer = _native_lines(dict(chunk))
            if lines and footer != str(page):
                continue
            article = last_article(without_heading(clean, chunk["heading_path"]))
            if not article:
                continue
            next_chunk = db.execute(
                _CHUNKS + " AND c.doc_id=? AND c.page_num=? AND c.heading_path=? AND c.type='text'"
                " ORDER BY c.id LIMIT 1", (chunk["doc_id"], page + 1, chunk["heading_path"]),
            ).fetchone()
            if not next_chunk or json.loads(next_chunk["bbox"])[1] > 0.25:
                continue
            following = next_chunk["seek_text"].partition("\n")[2]
            if re.match(r"\s*\d+(?:\.\d+)?\s*(?:元|%)[。；，]", following):
                pairs[chunk["id"]] = (dict(chunk), dict(next_chunk), clean, article)
    return pairs


def _page_evidence(hits: list[dict], question: str, doc_id: int | None) -> list[dict]:
    ids = [doc_id] if doc_id is not None else list(dict.fromkeys(hit["doc_id"] for hit in hits))[:2]
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    with connect() as db:
        rows = db.execute(
            _CHUNKS + f" AND c.doc_id IN ({marks}) AND c.type='text' AND c.page_num<=3"
            " AND c.heading_path='目录' ORDER BY c.page_num,c.id LIMIT 2", ids,
        ).fetchall()
        if "设计回访" in question:
            rows += db.execute(
                _CHUNKS + f" AND c.doc_id IN ({marks}) AND c.type='text'"
                " AND replace(c.heading_path, ' ', '')=?"
                " ORDER BY c.page_num,c.id LIMIT 4", (*ids, REPORT_HEADING),
            ).fetchall()
    return [dict(row) for row in rows]


async def _search(args: dict, citations: Citations, doc_id: int | None = None, *,
                  consider_editions: bool = False) -> dict:
    query = args.get("query", "")
    if not query:
        return {"count": 0, "blocks": [], "message": "请提供检索词。"}
    top_k = args.get("top_k", 6)
    question = citations.question.strip()
    asks_flow = (_FLOW_QUESTION.search(question) and _FLOW_ORDER.search(question)
                 and not _TABLE_ONLY.search(question))
    hits = (await retrieval.search(query, doc_id=doc_id, top_k=top_k))["results"]
    if consider_editions:
        editions = await asyncio.to_thread(current_editions, hits, question)
        if editions:
            edition_results = await asyncio.gather(*(
                retrieval.search(question, doc_id=edition, top_k=top_k) for edition in editions[:2]
            ))
            preferred = [chunk for result in edition_results for chunk in result["results"]]
            hits = list({chunk["id"]: chunk for chunk in [*preferred, *hits]}.values())[:top_k]
    if asks_flow and question != query:
        original = (await retrieval.search(question, doc_id=doc_id, top_k=top_k))["results"]
        found_docs = {chunk["doc_id"] for chunk in hits}
        terms = set(retrieval._fts_terms(question))
        figures = [chunk for chunk in original if chunk["type"] == "figure"
                   and (chunk.get("routes") == "附录标题" or (
                       (not found_docs or chunk["doc_id"] in found_docs)
                       and terms.intersection(retrieval._fts_terms(chunk["heading_path"]))))]
        merged = {}
        for chunk in [*figures[:1], *hits, *original]:
            merged.setdefault(chunk["id"], chunk)
        hits = list(merged.values())[:top_k]
    if not (asks_flow and hits and hits[0].get("routes") == "附录标题"):
        for chunk in reversed(await asyncio.to_thread(_literal_hits, question, doc_id)):
            hits = [chunk, *(item for item in hits if item["id"] != chunk["id"])][:top_k]
    signers = await asyncio.to_thread(_signing_clauses, hits, question) if "签署批准" in question else []
    signing_text = {chunk["id"]: text for chunk, text in signers}
    hits = list({chunk["id"]: chunk for chunk in [*(chunk for chunk, _ in signers), *hits]}.values())[:top_k]
    if PAGE_QUESTION.search(question):
        page_evidence = await asyncio.to_thread(_page_evidence, hits, question, doc_id)
        hits = list({chunk["id"]: chunk for chunk in [*hits, *page_evidence]}.values())
    continuations = await asyncio.to_thread(_find_search_continuations, hits)
    text_continuations = await asyncio.to_thread(_text_continuations, hits)
    continued_ids = {tail["id"] for _, tail, _, _ in text_continuations.values()}
    blocks = []
    seen = set()
    for chunk in hits:
        if chunk["id"] in seen or chunk["id"] in continued_ids:
            continue
        if chunk["id"] in continuations:
            header, body = continuations[chunk["id"]]
            if body["id"] not in seen:
                blocks.append({**citations.register(body), **_table_content(body, _html_rows(header)[0])})
                seen.add(body["id"])
            seen.add(chunk["id"])
        else:
            if chunk["id"] in text_continuations:
                head, tail, clean, article = text_continuations[chunk["id"]]
                blocks.append(_block(head, citations, query, clean))
                tail_text = tail["seek_text"].partition("\n")[2]
                blocks.append({**citations.register(tail, article),
                               "text": f"【{article}续】{tail_text}"})
                seen.add(tail["id"])
            else:
                blocks.append(_block(chunk, citations, f"{query} {question}", signing_text.get(chunk["id"])))
            seen.add(chunk["id"])
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


def _html_rows(chunk) -> list[list[str]]:
    parser = _TableRows()
    parser.feed(chunk["html"] or "")
    return parser.rows


def _named_table_row(chunk, question: str) -> list[str]:
    compact_question = re.sub(r"\s+", "", question)
    titles = set(_QUOTED_TITLE.findall(compact_question))
    matches = []
    for row in _html_rows(chunk):
        name = re.sub(r"\s+", "", row[0])
        quoted_match = any(re.sub(r"\s+", "", cell).strip("《》") in titles for cell in row)
        if (len(name) >= 3 and name in compact_question) or quoted_match:
            matches.append(row)
    return matches[0] if len(matches) == 1 else []


def _table_content(chunk, continued_header: list[str] | None = None) -> dict:
    html = chunk["html"] or ""
    parsed = _html_rows(chunk)
    if continued_header is None:
        city_headers = [row for row in parsed if "职级" in row and "一类城市" in row and "三类城市" in row]
        if len(city_headers) == 1 and parsed[0] != city_headers[0]:
            parsed = [city_headers[0], *(row for row in parsed if row != city_headers[0])]
    header = (parsed[0] if parsed else []) if continued_header is None else continued_header
    rows = parsed[1:] if continued_header is None else parsed
    if continued_header is not None:
        preview = "".join(f"<th>{escape(cell)}</th>" for cell in header)
        html = re.sub(r"<table>", f"<table><tr>{preview}</tr>", html, count=1)
    data = {"header": header, "rows": rows, "html": html}
    if not parsed:
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


def _full_table(db, chunk):
    if chunk["type"] == "table_full":
        return chunk
    return db.execute(
        _CHUNKS + " AND c.doc_id=? AND c.page_num=? AND c.bbox=?"
        " AND c.type='table_full' AND c.id<? ORDER BY c.id DESC LIMIT 1",
        (chunk["doc_id"], chunk["page_num"], chunk["bbox"], chunk["id"])).fetchone()


def _continued_table(db, table):
    rows = _html_rows(table)
    if not rows or not table["heading_path"]:
        return None
    x0, y0, x1, y1 = json.loads(table["bbox"])
    numeric = re.compile(r"[\d,.]+(?:\s*[元%])?")
    is_header = len(rows) == 1 and all(cell and not numeric.fullmatch(cell) for cell in rows[0])
    if is_header and y1 >= 0.78:
        page = table["page_num"] + 1
    elif y0 <= 0.22:
        page = table["page_num"] - 1
    else:
        return None
    candidates = db.execute(
        _CHUNKS + " AND c.doc_id=? AND c.page_num=? AND c.type='table_full' AND c.heading_path=?",
        (table["doc_id"], page, table["heading_path"]),
    ).fetchall()
    pairs = []
    for other in candidates:
        ox0, oy0, ox1, oy1 = json.loads(other["bbox"])
        if max(abs(x0 - ox0), abs(x1 - ox1)) > 0.04:
            continue
        header, body = (table, other) if is_header else (other, table)
        header_rows, body_rows = _html_rows(header), _html_rows(body)
        if (is_header and oy0 > 0.22) or (not is_header and oy1 < 0.78):
            continue
        if (len(header_rows) == 1 and len(header_rows[0]) >= 2 and body_rows
                and all(cell and not numeric.fullmatch(cell) for cell in header_rows[0])
                and all(len(row) == len(header_rows[0]) and any(numeric.fullmatch(c) for c in row[1:])
                        for row in body_rows)):
            pairs.append((header, body))
    return pairs[0] if len(pairs) == 1 else None


def _find_search_continuations(hits: list[dict]) -> dict:
    relevant = [chunk for chunk in hits if chunk["type"] in ("table_full", "table_row")]
    if not relevant:
        return {}
    pairs = {}
    with connect() as db:
        for chunk in relevant:
            # 检索对象可能来自旧索引快照，按原块重新读取坐标。
            base = _get_chunk(db, {"chunk_id": chunk["id"]})
            if base is not None and (full := _full_table(db, base)) is not None:
                if pair := _continued_table(db, full):
                    pairs[chunk["id"]] = pair
    return pairs


def _read_table(args: dict):
    with connect() as db:
        chunk = _get_chunk(db, args)
        if chunk is None or chunk["type"] not in {"table_row", "table_full"}:
            return None, None
        table = _full_table(db, chunk)
        return (table, _continued_table(db, table)) if table is not None else (None, None)


async def read_table(args: dict, citations: Citations) -> dict:
    scope, error = await asyncio.to_thread(requested_document, citations.question)
    if error:
        return {"message": error}
    if scope is not None:
        args = {**args, "doc_id": scope}
    table, pair = await asyncio.to_thread(_read_table, args)
    if table is None:
        return {"message": "找不到对应的整表，请使用有效的表格块 ID。"}
    if pair is not None:
        header, body = pair
        blocks = [{**citations.register(header), **_table_content(header)},
                  {**citations.register(body), **_table_content(body, _html_rows(header)[0])}]
        return {"count": len(blocks), "blocks": blocks}
    return {"count": 1, "blocks": [{**citations.register(table), **_table_content(table)}]}


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
    scope, error = await asyncio.to_thread(requested_document, citations.question)
    if error:
        return {"message": error}
    if scope is not None:
        args = {**args, "doc_id": scope}
    neighbors = await asyncio.to_thread(_read_neighbors, args)
    if neighbors is None:
        return {"message": "找不到可查阅的资料块。"}
    before, after = neighbors
    blocks = [{**_block(chunk, citations), "position": position}
              for position, rows in (("before", before), ("after", after)) for chunk in rows]
    return {"count": len(blocks), "blocks": blocks}


async def read_figure(args: dict, citations: Citations) -> dict:
    scope, error = await asyncio.to_thread(requested_document, citations.question)
    if error:
        return {"message": error}
    if scope is not None:
        args = {**args, "doc_id": scope}
    figure = await asyncio.to_thread(_open_chunk, args)
    if figure is None or figure["type"] != "figure":
        return {"message": "找不到对应的流程图，请使用有效的流程图块 ID。"}
    description = figures.verified_description(figure) or figure["vl_desc"] or ""
    node_text = figure["linear_text"] or ("" if description else figure["seek_text"].partition("\n")[2])
    block = {**citations.register(figure), "node_text": node_text[:1500],
             "structure": description[:_FIGURE_MAX_CHARS]}
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
