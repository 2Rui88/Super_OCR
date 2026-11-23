"""证件线流水线：OCR 行 → 类型分类 → 字段组装 → 校验 → CardResult。

组装策略（全部由 YAML 配置驱动）：
- same_line：锚点词出现在行内，值 = 锚点右侧剩余文本（或按 pattern 提取）；
- next_line：值在锚点下方最近一行（y 更大、x 有重叠），multi_line 字段继续向下吞行，
  直到撞上其他字段的锚点行；
- 字段置信度 = 所有参与行置信度的 min（短板效应，宁可保守）；
- bbox = 参与行的联合框（加分项：原图画框）。
"""

from __future__ import annotations

#: 组装逻辑版本：进 worker 的结果缓存键——逻辑升级（如锚点容错）后，
#: 旧缓存自然失效，不会把升级前的结果当命中返回。
PIPELINE_VERSION = 2

import hashlib
import re
import time
from pathlib import Path

from app.certs import CertConfig, classify, run_cross_check
from app.certs import validators as V
from app.contracts import CardResult, FieldResult, FieldValidation
from app.engine.base import OcrLine

CONFIDENCE_REVIEW_THRESHOLD = 0.7  # 低于此值标"建议人工复核"


def _find_anchor_line(lines: list[OcrLine], anchors: list[str],
                      all_anchor_texts: set[str] | None = None,
                      ) -> tuple[int, str, int] | None:
    """找到包含任一锚点词的行，返回 (行号, 命中的锚点词, 行内匹配起点)。

    长锚点优先（先试'出生日期'再'出生'）。精确匹配失败后退到容错匹配
    （_fuzzy_anchor_hit）——透视/噪点等畸变下单字误认（如'注册资本'→'主册资本'）
    不至于让整个字段落空。"""
    for anchor in sorted(anchors, key=len, reverse=True):
        for i, line in enumerate(lines):
            idx = line.text.find(anchor)
            if idx >= 0:
                return i, anchor, idx
    return _fuzzy_anchor_hit(lines, anchors, all_anchor_texts)


def _levenshtein(a: str, b: str) -> int:
    """编辑距离（中文按单字比较）。锚点仅 2~8 字，O(n·m) 开销可忽略。"""
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _fuzzy_anchor_hit(lines: list[OcrLine], anchors: list[str],
                      all_anchors: set[str] | None = None,
                      ) -> tuple[int, str, int] | None:
    """容错锚点匹配：行内滑窗（窗口=锚点字数）与锚点比编辑距离。

    阈值：锚点 <6 字容错 1 字、≥6 字容错 2 字（如'统一社会信用代码'）。
    - 本字段多个别名同分 → 取更长（更具体）的锚点；
    - all_anchors（全体字段的锚点）给出时做跨字段防抢：命中窗口若与其他
      字段的锚点同样近或更近，视为歧义整体放弃——宁可空值人工复核，
      不能张冠李戴。
    """
    best: tuple[int, int, str, int] | None = None   # (距离, 行号, 锚点, 起点)
    for anchor in sorted(anchors, key=len, reverse=True):
        limit = 1 if len(anchor) < 6 else 2
        for i, line in enumerate(lines):
            text = line.text
            for start in range(0, max(1, len(text) - len(anchor) + 1)):
                d = _levenshtein(text[start:start + len(anchor)], anchor)
                if d > limit:
                    continue
                if (best is None or d < best[0]
                        or (d == best[0] and len(anchor) > len(best[2]))):
                    best = (d, i, anchor, start)
    if best is None:
        return None
    if all_anchors:
        own = set(anchors)
        text = lines[best[1]].text
        for other in all_anchors - own:
            limit = 1 if len(other) < 6 else 2
            for s in range(0, max(1, len(text) - len(other) + 1)):
                if _levenshtein(text[s:s + len(other)], other) <= min(limit, best[0]):
                    return None    # 其他字段同样近/更近 → 歧义放弃
    return best[1], best[2], best[3]


def _strip_anchor(text: str, anchor: str, start: int | None = None) -> str:
    """去掉锚点及其后紧邻的分隔符（冒号/空格）。start 为容错匹配的行内起点。"""
    idx = text.find(anchor) if start is None else start
    rest = text[idx + len(anchor):] if idx >= 0 else text
    return rest.lstrip("：: ，,、\t ")


def _same_row(a: OcrLine, b: OcrLine) -> bool:
    """两行是否处于同一视觉行：垂直重叠 ≥ 两者高度较小者的 50%。"""
    overlap = min(a.bbox[3], b.bbox[3]) - max(a.bbox[1], b.bbox[1])
    min_h = max(1e-6, min(a.bbox[3] - a.bbox[1], b.bbox[3] - b.bbox[1]))
    return overlap / min_h >= 0.5


#: 类型默认取值正则：无显式 pattern 时按字段类型从候选文本中提取
_TYPE_PATTERNS: dict[str, str] = {
    "chinese_id": r"\d{17}[0-9Xx]|\d{15}",
    "uscc": r"[0-9A-HJ-NPQRTUWXY]{18}",
    "date": r"\d{4}\s*[年./-]\s*\d{1,2}\s*[月./-]\s*\d{1,2}\s*日?|\d{8}",
    "period": (
        r"\d{4}\s*[年./-]\s*\d{1,2}\s*[月./-]\s*\d{1,2}\s*日?\s*[-—至~]\s*"
        r"(?:\d{4}\s*[年./-]\s*\d{1,2}\s*[月./-]\s*\d{1,2}\s*日?|长期)"
    ),
}


def _value_pattern(fc) -> str | None:
    if fc.pattern:
        return fc.pattern
    return _TYPE_PATTERNS.get(fc.type)


def _line_below(lines: list[OcrLine], idx: int, anchor: OcrLine) -> OcrLine | None:
    """锚点行下方最近的一行：y 更大、与锚点行 x 范围有重叠。"""
    best: OcrLine | None = None
    for ln in lines:
        if ln.bbox[1] <= anchor.bbox[1]:
            continue
        x_overlap = min(ln.bbox[2], anchor.bbox[2]) - max(ln.bbox[0], anchor.bbox[0])
        if x_overlap <= 0:
            continue
        if best is None or ln.bbox[1] < best.bbox[1]:
            best = ln
    return best


def _lines_right_of(lines: list[OcrLine], anchor: OcrLine) -> list[OcrLine]:
    """与锚点同一视觉行、且在锚点右侧的行（证件"标签|值"左右并排版式），按 x 排序。"""
    right = [
        ln for ln in lines
        if ln is not anchor and _same_row(ln, anchor)
        and ln.bbox[0] >= anchor.bbox[2] - 0.01
    ]
    return sorted(right, key=lambda l: l.bbox[0])


def _union_bbox(bboxes: list[list[float]]) -> list[float]:
    if not bboxes:
        return [0.0, 0.0, 0.0, 0.0]
    return [
        min(b[0] for b in bboxes), min(b[1] for b in bboxes),
        max(b[2] for b in bboxes), max(b[3] for b in bboxes),
    ]


def _absorb_below(
    lines: list[OcrLine],
    start: OcrLine,
    all_anchor_texts: set[str],
    claimed: set[int],
) -> list[OcrLine]:
    """从 start 向下吞行（多行值续行）。

    停止条件：没有下一行 / 撞到其他字段锚点 / 垂直间距 > 1.5 倍行高（跳段，
    防止吞掉页脚水印）。已被单行字段认领的行跳过但继续（它是别人的值，不是续行边界）。
    """
    absorbed: list[OcrLine] = []
    cur = start
    while cur in lines:
        cur_h = max(1e-6, cur.bbox[3] - cur.bbox[1])
        nxt = _line_below(lines, lines.index(cur), cur)
        if nxt is None or any(
            any(a in row.text for a in all_anchor_texts) and _same_row(row, nxt)
            for row in lines
        ):
            break
        if nxt.bbox[1] - cur.bbox[3] > 1.5 * cur_h:
            break  # 段落间距过大，不是连续续行
        if id(nxt) not in claimed:
            absorbed.append(nxt)
        cur = nxt
    return absorbed


def _extract_field(
    lines: list[OcrLine],
    idx: int,
    anchor_word: str,
    anchor_start: int,
    fc,
    all_anchor_texts: set[str],
    claimed: set[int],
) -> tuple[str, list[OcrLine]]:
    """单个字段取值。优先级：同行剩余 → 同行右侧 → 下方（多行字段继续吞行）。

    证件版式两类：标签与值同一行（"姓名张三"）、标签与值并排成两个检测行
    （OCR 输出"姓名"和"张三"两行、同一视觉行）——后者靠 _lines_right_of 命中。
    """
    import re as _re

    anchor_line = lines[idx]
    rest = _strip_anchor(anchor_line.text, anchor_word, anchor_start).strip()
    right = [
        l for l in _lines_right_of(lines, anchor_line)
        if not any(a in l.text for a in all_anchor_texts)
    ]
    right_text = "".join(l.text for l in right).strip()
    below = _line_below(lines, idx, anchor_line)
    if below is not None and any(a in below.text for a in all_anchor_texts):
        below = None
    below_text = below.text.strip() if below else ""

    pat = _value_pattern(fc)
    if pat:
        # 模式字段（证号/代码/日期/时间段）：按优先级在候选文本里搜模式
        for source, used in (
            (rest, [anchor_line]),
            (right_text, right),
            (below_text, [below] if below else []),
            (anchor_line.text, [anchor_line]),
        ):
            if not source:
                continue
            m = _re.search(pat, source)
            if m:
                return m.group(0), used
        return "", []

    # 普通文本字段：剩余 → 右侧（含右侧列续行）→ 下方（含多行吞行）
    if rest:
        used = [anchor_line]
        if fc.multi_line:
            used += _absorb_below(lines, anchor_line, all_anchor_texts, claimed)
        return rest + "".join(line.text for line in used[1:]), used
    if right:
        used = list(right)
        if fc.multi_line:
            used += _absorb_below(lines, right[-1], all_anchor_texts, claimed)
        return "".join(l.text for l in used).strip(), used
    if below:
        used = [below]
        if fc.multi_line:
            used += _absorb_below(lines, below, all_anchor_texts, claimed)
        return (below_text + "".join(l.text for l in used[1:])).strip(), used
    return "", []


def _clean_value(value: str, fc) -> str:
    """剔除水印噪声词（默认'样本'，YAML 可配）。'男样本' → '男'。"""
    for w in fc.stop_words:
        value = value.replace(w, "")
    return value.strip()


def assemble_fields(lines: list[OcrLine], cfg: CertConfig) -> list[FieldResult]:
    """按 YAML 配置从 OCR 行组装全部字段。两遍策略：

    第一遍处理单行字段并"认领"其值行（claimed），第二遍处理多行字段——
    多行吸收时跳过已被认领的行，避免把别的字段的值吞进来。
    """
    # Large watermark boxes can overlap several visual rows. Ignore standalone
    # configured noise before spatial matching, not only after value extraction.
    noise_words = {word for field in cfg.fields for word in field.stop_words}
    lines = [line for line in lines if line.text.strip() not in noise_words]
    all_anchor_texts = {a for f in cfg.fields for a in f.anchors}
    claimed: set[int] = set()
    extracted: dict[str, tuple[str, list[OcrLine]]] = {}

    def run(fc) -> None:
        hit = _find_anchor_line(lines, fc.anchors, all_anchor_texts)
        if hit is None:
            extracted[fc.name] = ("", [])
            return
        i, anchor_word, start = hit
        extracted[fc.name] = _extract_field(
            lines, i, anchor_word, start, fc, all_anchor_texts, claimed
        )

    # 第一遍：单行字段（先认领）
    for fc in cfg.fields:
        if not fc.multi_line:
            run(fc)
            _, used = extracted[fc.name]
            claimed.update(id(l) for l in used)
    # 第二遍：多行字段（吸收时避开已认领行）
    for fc in cfg.fields:
        if fc.multi_line:
            run(fc)

    results: list[FieldResult] = []
    for fc in cfg.fields:
        raw_value, used_lines = extracted.get(fc.name, ("", []))
        value = _clean_value(raw_value, fc)
        conf = min((l.confidence for l in used_lines), default=0.0)
        results.append(FieldResult(
            name=fc.name,
            value=value,
            confidence=round(conf, 4),
            bbox=_union_bbox([l.bbox for l in used_lines]) if used_lines else None,
        ))
    return results


def validate_fields(fields: list[FieldResult], cfg: CertConfig) -> None:
    """就地执行字段级校验 + 交叉校验，写回 validation / needs_review。"""
    by_name = {f.name: f for f in fields}
    raw_values = {f.name: f.value for f in fields}

    # 字段类型校验
    for fc in cfg.fields:
        fr = by_name.get(fc.name)
        if fr is None or not fr.value:
            continue
        if fc.type == "enum" and fc.allowed:
            ok, errs = V.validate_enum(fr.value, fc.allowed)
        else:
            ok, errs = V.run_validator(fc.type, fr.value)
        if not ok:
            fr.validation = FieldValidation(passed=False, errors=errs)

    # 交叉校验（出生/性别与证号一致、起止顺序等）
    for check in cfg.cross_checks:
        target, errs = run_cross_check(check, raw_values)
        if target and errs and target in by_name:
            fr = by_name[target]
            fr.validation = FieldValidation(
                passed=False, errors=fr.validation.errors + errs
            )

    # 汇总 needs_review
    for fr in fields:
        fr.needs_review = (not fr.validation.passed) or fr.confidence < CONFIDENCE_REVIEW_THRESHOLD


def build_card_result(
    path: str | Path,
    lines: list[OcrLine],
    registry: dict[str, CertConfig],
    *,
    sha256: str = "",
    hint_type: str | None = None,
    doc_type: str | None = None,
    conf: float | None = None,
    started: float | None = None,
) -> CardResult:
    """从已有 OCR 行组装 CardResult（分类 → 组装 → 校验）。

    与识别解耦：图片路由一次识别后，命中证件走这里复用行结果，
    不再做第二次 OCR。doc_type 已知时直接传入（conf 一并给）。
    """
    path = Path(path)
    t0 = started if started is not None else time.perf_counter()
    digest = sha256 or hashlib.sha256(path.read_bytes()).hexdigest()
    full_text = "\n".join(l.text for l in lines)

    if doc_type is None:
        if hint_type and hint_type in registry:
            doc_type, conf = hint_type, 1.0
        else:
            doc_type, conf = classify(full_text, registry)
    elif conf is None:
        conf = 1.0

    if doc_type == "unknown":
        return CardResult(
            file_sha256=digest, filename=path.name,
            doc_type="unknown", type_confidence=0.0,
            issues=["未能识别证件类型：不属于支持的4类证件或特征不足，请人工确认"],
            duration_ms=int((time.perf_counter() - t0) * 1000),
        )

    cfg = registry[doc_type]
    fields = assemble_fields(lines, cfg)
    validate_fields(fields, cfg)
    issues = [
        f"{f.name}：{'；'.join(f.validation.errors)}"
        for f in fields if not f.validation.passed
    ] + [
        f"{f.name}：置信度低（{f.confidence}），建议人工复核"
        for f in fields if f.validation.passed and f.needs_review
    ]

    return CardResult(
        file_sha256=digest, filename=path.name,
        doc_type=doc_type, doc_type_cn=cfg.name, type_confidence=conf,
        fields=fields, issues=issues,
        duration_ms=int((time.perf_counter() - t0) * 1000),
    )

