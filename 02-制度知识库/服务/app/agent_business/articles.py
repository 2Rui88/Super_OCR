"""为旧文档证据标出条款归属，保留原块 ID 和页码。"""
import re

from app.db import connect

_ARTICLE = re.compile(r"(?:^|(?<=[\n。！？；]))[ \t]*(第[一二三四五六七八九十百零〇\d]+条)")
_CONTINUATION = re.compile(r"^【(第[一二三四五六七八九十百零〇\d]+条)续】")
_ITEM = re.compile(r"(?:^|(?<=[\n。！？；]))[ \t]*[（(]([一二三四五六七八九十\d]+)[）)]")


def without_heading(text: str, heading: str) -> str:
    if heading and not _ARTICLE.match(heading) and text.startswith(heading):
        return text[len(heading):].lstrip()
    return text


def last_article(text: str) -> str:
    return next((match.group(1) for match in reversed(list(_ARTICLE.finditer(text)))), "")


def label_articles(chunk, text: str) -> str:
    markers = list(_ARTICLE.finditer(text))
    if not markers and not re.match(r"^\s*[（(][一二三四五六七八九十\d]+[）)]", text):
        return text
    prefix = text[:markers[0].start(1)] if markers else text
    if _ARTICLE.match(text):
        return text
    if prefix.strip() and chunk["heading_path"]:
        with connect() as db:
            previous = db.execute(
                "SELECT seek_text FROM chunks WHERE doc_id=? AND heading_path=? AND type='text'"
                " AND id<? AND page_num BETWEEN ? AND ? ORDER BY id DESC LIMIT 8",
                (chunk["doc_id"], chunk["heading_path"], chunk["id"],
                 max(1, chunk["page_num"] - 1), chunk["page_num"]),
            ).fetchall()
        for row in previous:
            prior = list(_ARTICLE.finditer(row["seek_text"].partition("\n")[2]))
            if prior:
                label = prior[-1].group(1)
                if markers:
                    boundary = markers[0].start(1)
                    return f"【{label}续】{prefix.rstrip()}\n【新条款】{text[boundary:]}"
                return f"【{label}续】{text}"
    return text


def select_clause(text: str, terms: list[str]) -> tuple[str, str]:
    """从正文而非条文中的交叉引用提取与问题对应的条款。"""
    continued = _CONTINUATION.match(text)
    spans = []
    if continued:
        article = continued.group(1)
        first, _, following = text[continued.end():].partition("\n【新条款】")
        items = list(_ITEM.finditer(first))
        for index, item in enumerate(items):
            end = items[index + 1].start() if index + 1 < len(items) else len(first)
            spans.append((f"{article}第（{item.group(1)}）项", first[item.start():end].strip()))
        if not items:
            spans.append((article, first.strip()))
    else:
        following = text
    articles = list(_ARTICLE.finditer(following))
    for index, next_article in enumerate(articles):
        end = articles[index + 1].start() if index + 1 < len(articles) else len(following)
        spans.append((next_article.group(1), following[next_article.start():end].strip()))
    if len(spans) == 1 and _ARTICLE.match(text):
        return text, spans[0][0]
    if not continued:
        return text, ""
    ranked = sorted(((sum(term in label + body for term in terms), label, body) for label, body in spans),
                    reverse=True)
    if ranked and ranked[0][0] >= 2 and (len(ranked) == 1 or ranked[0][0] > ranked[1][0]):
        _, label, body = ranked[0]
        return f"【{label}】{body}", label
    return text, ""
