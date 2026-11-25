"""为当前制度问题找到有明确施行日期的相关修订文件。"""
from datetime import date
import re

from app.agent_business.document_scope import _title_key, _years
from app.db import connect

_HISTORICAL = re.compile(r"(?:19|20)\d{2}\s*(?:年|版|修订|[./-]\d)|旧版|当时|之前|对比|相比|历史")
_EFFECTIVE = re.compile(r"自\s*(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日[起开始]*\s*(?:施行|实施|生效)")
_REPEAL = re.compile(r"《([^》]+)》[^。；\n]{0,45}废止")


def current_editions(hits: list[dict], question: str) -> list[int]:
    if not hits or _HISTORICAL.search(question):
        return []
    sources = {hit["doc_id"]: hit["filename"] for hit in hits if "doc_id" in hit}
    if not sources:
        return []
    selected = {}
    with connect() as db:
        rows = db.execute(
            "SELECT d.id, d.filename, c.seek_text FROM chunks c JOIN documents d ON c.doc_id=d.id"
            " WHERE d.status='ready' AND d.superseded_by IS NULL AND c.type='text'"
            " AND (instr(c.seek_text,'施行') OR instr(c.seek_text,'实施') OR instr(c.seek_text,'生效'))"
        )
        today = date.today()
        for doc_id, filename, evidence in rows:
            match = _EFFECTIVE.search(evidence)
            if not match:
                continue
            try:
                start = date(*(int(part) for part in match.groups()))
            except ValueError:
                continue
            if start > today:
                continue
            repealed = {_title_key(name) for name in _REPEAL.findall(evidence)}
            key = _title_key(filename)
            for old_id, old in sources.items():
                if old_id == doc_id:
                    continue
                previous = _title_key(old)
                years = _years(old)
                close_title = key == previous or key.replace("使用管理规定", "管理规定") == previous
                if previous in repealed or (close_title and years and max(years) < str(start.year)):
                    selected[doc_id] = start
                    break
    return sorted(selected, key=selected.get, reverse=True)
