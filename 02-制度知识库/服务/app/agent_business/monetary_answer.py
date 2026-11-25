"""落实已核对的计算和引用，再生成用户可见的答案。"""
import json
import re
from contextlib import aclosing
from decimal import Decimal, InvalidOperation

from app import llm

_SUM_QUESTION = re.compile(r"(?:合计|总计|总共|一共).*?(?:多少|几元)")
_CITATION = re.compile(r"(?m)^\[(\d+)] ")
_REF = re.compile(r"\[(\d+)]")
_ARTICLE = re.compile(r"第[一二三四五六七八九十百零〇\d]+条")
_RULES = (
    "本题的总额由程序计算。根据编号资料，只输出 JSON 对象，key 为 items。"
    '每项结构为 {"label":"费用名称","rate":100,"count":3,"deductions":'
    '[{"rate":30,"count":2}],"refs":[1,2],"basis":"适用条件的简短说明"}。'
    "rate 和 count 都写非负 JSON 数字，定额直接用 count=1，无扣减时 deductions=[]。"
    "每个费用项目都要包含，只使用适用的同一版制度，注明所用引用。"
    "出差住宿要查清城市档次和对应职级，住宿按晚数，日补贴按其条文规定的日数。"
    "basis 只写条件与类别，勿写金额、计算过程或结论。"
    '若资料不足则返回 {"items":[]}。不要输出 Markdown 或解释。'
)


def _decimal(value) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or not 0 <= value <= 10**12:
        raise ValueError("金额或数量无效")
    return value


def _product(entry) -> tuple[Decimal, str]:
    rate, count = _decimal(entry["rate"]), _decimal(entry["count"])
    return rate * count, f"{_format(rate)} × {_format(count)}"


def _format(value: Decimal) -> str:
    result = format(value, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def render_amounts(raw: str, allowed_refs: set[int]) -> str:
    payload = json.loads(raw, parse_int=Decimal, parse_float=Decimal)
    items = payload["items"]
    if not isinstance(items, list) or len(items) > 12:
        raise ValueError("金额项目无效")
    if not items:
        return "没有找到相关规定"
    total = Decimal(0)
    labels = set()
    lines = []
    for item in items:
        label = item["label"].strip()
        basis = item["basis"].strip()
        refs = item["refs"]
        deductions = item["deductions"]
        if (not label or label in labels or len(label) > 40 or len(basis) > 160
                or not isinstance(refs, list) or not refs or any(ref not in allowed_refs for ref in refs)
                or not isinstance(deductions, list) or len(deductions) > 12):
            raise ValueError("金额项目或引用无效")
        labels.add(label)
        amount, expression = _product(item)
        for deduction in deductions:
            value, deduction_expression = _product(deduction)
            amount -= value
            expression += f" − {deduction_expression}"
        if amount < 0:
            raise ValueError("扣减超过项目金额")
        total += amount
        sources = "".join(f"[{int(ref)}]" for ref in dict.fromkeys(refs))
        note = f"（{basis}）" if basis else ""
        lines.append(f"- {label}{note}：{expression} = {_format(amount)} 元 {sources}")
    return f"合计 **{_format(total)} 元**。\n\n" + "\n".join(lines)


def checked_reference_line(line: str, allowed: set[int], articles: dict[str, set[int]]) -> str:
    mentioned = set(_ARTICLE.findall(line))
    candidates = articles.get(next(iter(mentioned)), set()) & allowed if len(mentioned) == 1 else set()
    replacement = f"[{next(iter(candidates))}]" if len(candidates) == 1 else ""
    return _REF.sub(lambda ref: ref[0] if int(ref[1]) in allowed else replacement, line)


def calculation_stream(question: str, citations):
    asks_sum = _SUM_QUESTION.search(question) and re.search(r"费|补助|报销|金额", question)

    async def stream(messages, *, tools):
        final = not tools and messages[-1]["content"].startswith("资料：")
        if final and citations.computed_answer:
            yield citations.computed_answer
            return
        if not final:
            async with aclosing(llm.chat_stream(messages, tools=tools)) as response:
                async for fragment in response:
                    yield fragment
            return
        allowed = {int(ref) for ref in _CITATION.findall(messages[-1]["content"])}
        if not asks_sum:
            pending = ""
            async with aclosing(llm.chat_stream(messages, tools=tools)) as response:
                async for fragment in response:
                    if not citations.article_refs or not isinstance(fragment, str):
                        yield fragment
                        continue
                    lines = (pending + fragment).split("\n")
                    pending = lines.pop()
                    if lines:
                        yield "\n".join(checked_reference_line(line, allowed, citations.article_refs)
                                        for line in lines) + "\n"
            if pending:
                yield checked_reference_line(pending, allowed, citations.article_refs)
            return
        prompt = [{**messages[0], "content": messages[0]["content"] + "\n" + _RULES}, *messages[1:]]
        raw = await llm.chat(prompt, temperature=0, max_tokens=900)
        try:
            yield render_amounts(raw, allowed)
        except (AttributeError, InvalidOperation, KeyError, TypeError, ValueError):
            yield {"finish_reason": "invalid_amount_breakdown"}

    return stream
