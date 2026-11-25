"""按用户指定的文件和版本限定知识库查阅范围。"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

from app.db import connect

_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_TITLE = re.compile(r"《([^》]+)》")
_DOCUMENT_WORD = re.compile(r"办法|规定|规程|程序|手册|制度|汇编|规范|细则")
_ONLY_SOURCE = re.compile(r"(?:仅|只)(?:限|按|用|查(?:阅)?|依据|根据|参考|看|检索|在|从|使用)+\s*$")
_SOURCE_INTRO = re.compile(r"(?:按(?:照)?|依照|依据|根据)[^，。！？《》]{0,24}$")
_SOURCE_CONTENT = re.compile(r"^(?:[（(](?:19|20)\d{2}(?:年|版)*[）)])?\s*(?:中|里|内|第|附录)")
_REFERENCE = re.compile(r"(?:提到|引用|列出|列明|提及|转引)的?\s*$")


def _years(text: str) -> set[str]:
    return set(_YEAR.findall(unicodedata.normalize("NFKC", text)))


def _title_key(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold().strip()
    text = re.sub(r"\.(?:pdf|docx?)$", "", text)
    text = _YEAR.sub("", text)
    text = re.sub(r"关于印发|的通知|盖章扫描件|扫描件|扫描|修订版|年版|版$", "", text)
    text = re.sub(r"[^\w]", "", text)
    return text.removesuffix("年").removesuffix("版")


@lru_cache(maxsize=48)
def _pdf_digest(path: str, size: int, modified_at: int) -> bytes:
    """比较已入库 PDF 每页的可见内容和文字，忽略容器与元数据差异。"""
    import pymupdf

    digest = hashlib.sha256()
    with pymupdf.open(path) as pdf:
        digest.update(f"{len(pdf)}\n".encode())
        for page in pdf:
            digest.update(re.sub(r"\s+", "", page.get_text()).encode("utf-8"))
            pixels = page.get_pixmap(dpi=100, alpha=False)
            digest.update(f"{pixels.width}x{pixels.height}\n".encode())
            digest.update(pixels.samples_mv)
    return digest.digest()


def _duplicate_document(rows: list[dict]) -> int | None:
    if len({row["filename"].casefold() for row in rows}) != 1:
        return None
    if len({row["sha256"] for row in rows}) == 1:
        return max(row["id"] for row in rows)
    try:
        signatures = set()
        for row in rows:
            path = Path(row["pdf_path"] or "")
            stat = path.stat()
            signatures.add(_pdf_digest(str(path.resolve()), stat.st_size, stat.st_mtime_ns))
    except (OSError, RuntimeError, ValueError):
        return None
    return max(row["id"] for row in rows) if len(signatures) == 1 else None


def _source_title(question: str, *, explicit_only: bool = False) -> str:
    candidates = set()
    for match in _TITLE.finditer(question):
        title = match.group(1)
        if not _DOCUMENT_WORD.search(title):
            continue
        before, after = question[:match.start()], question[match.end():]
        if _ONLY_SOURCE.search(before):
            return title
        if (not explicit_only and not _REFERENCE.search(before)
                and (_SOURCE_INTRO.search(before) or _SOURCE_CONTENT.match(after))):
            candidates.add(title)
    return next(iter(candidates)) if len(candidates) == 1 else ""


def _resolve(doc_id: int | None, name: str, question: str, *, exact: bool = False) -> tuple[int | None, str]:
    if doc_id is None and not name:
        return None, "" if not question else "请提供制度文件名或文档 ID。"
    original_years = _years(question)
    versions = _years(name)
    if exact and not versions and len(original_years) == 1:
        versions = original_years
    with connect() as db:
        rows = [dict(row) for row in db.execute(
            "SELECT id, filename, sha256, pdf_path FROM documents"
            " WHERE status='ready' AND superseded_by IS NULL"
        )]
        headings = db.execute(
            "SELECT DISTINCT doc_id, heading_path FROM chunks WHERE page_num<=2 AND heading_path<>''"
        ).fetchall() if name else []
    aliases = {}
    for heading in headings:
        aliases.setdefault(heading["doc_id"], []).append(heading["heading_path"])
    key = _title_key(name)
    matches = []
    for row in rows:
        titles = [row["filename"], *aliases.get(row["id"], [])]
        found_years = _years(row["filename"]) or set().union(*(_years(title) for title in titles))
        if versions and not versions.intersection(found_years):
            continue
        keys = {_title_key(title) for title in titles}
        if exact and key not in keys:
            continue
        if not exact and name and key not in keys and not (
            key and any(key in title for title in keys)
        ):
            continue
        if doc_id is not None and row["id"] != doc_id:
            continue
        matches.append(row)
    if not matches:
        return None, "没有找到与指定文件或版本匹配的可查阅文件。"
    if len(matches) == 1:
        return matches[0]["id"], ""
    equivalent = _duplicate_document(matches)
    if equivalent is not None:
        return equivalent, ""
    choices = "、".join(f"{row['filename']}（ID {row['id']}）" for row in matches[:5])
    return None, f"文件名匹配到多份不同制度，请使用文档 ID。候选文件：{choices}"


def requested_document(question: str) -> tuple[int | None, str]:
    """用户明确限制为单一文件时，搜索和读取均遵守该范围。"""
    title = _source_title(question, explicit_only=True)
    return _resolve(None, title, question, exact=True) if title else (None, "")


def preferred_document(question: str) -> int | None:
    """使用能映射到已入库文件的来源提示，无法解析时仍可搜索内容。"""
    title = _source_title(question)
    return _resolve(None, title, question, exact=True)[0] if title else None


def resolve_document(doc_id: int | None, name: str, question: str = "") -> tuple[int | None, str]:
    """解析工具选定的文件，保留用户明确给出的来源和版本。"""
    scoped, error = requested_document(question)
    if scoped is not None or error:
        return scoped, error
    if (preferred := preferred_document(question)) is not None:
        return preferred, ""
    return _resolve(doc_id, name, question)
