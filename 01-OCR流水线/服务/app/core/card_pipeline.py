"""证件线流水线：OCR 行 → 类型分类 → 字段组装 → 校验 → CardResult。

组装策略（全部由 YAML 配置驱动）：
- same_line：锚点词出现在行内，值 = 锚点右侧剩余文本（或按 pattern 提取）；
- next_line：值在锚点下方最近一行（y 更大、x 有重叠），multi_line 字段继续向下吞行，
  直到撞上其他字段的锚点行；
- 字段置信度 = 所有参与行置信度的 min（短板效应，宁可保守）；
- bbox = 字段值的文字区域联合框，同行标签不参与画框。
"""

from __future__ import annotations

#: 组装逻辑版本：进 worker 的结果缓存键——逻辑升级（如锚点容错）后，
#: 旧缓存自然失效，不会把升级前的结果当命中返回。
PIPELINE_VERSION = 9

import hashlib
import logging
import re
import time
from collections.abc import Callable
from pathlib import Path
from unicodedata import east_asian_width

from app.certs import CertConfig, classify, run_cross_check
from app.certs import validators as V
from app.certs.loader import FieldConfig
from app.contracts import CardResult, FieldResult, FieldValidation
from app.engine.base import OcrLine

log = logging.getLogger(__name__)
CONFIDENCE_REVIEW_THRESHOLD = 0.7  # 低于此值标"建议人工复核"
RegionRecognizer = Callable[[list[float], bool], list[OcrLine]]


def _find_anchor_line(lines: list[OcrLine], anchors: list[str],
                      all_anchor_texts: set[str] | None = None,
                      *, exact_only: bool = False,
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
    if exact_only:
        return None
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


_LEGACY_USCC_PATTERN = r"[0-9A-HJ-NPQRTUWXY]{18}"

#: 类型默认取值正则：无显式 pattern 时按字段类型从候选文本中提取
_TYPE_PATTERNS: dict[str, str] = {
    "chinese_id": r"\d{17}[0-9Xx]|\d{15}",
    # 提取完整候选，字符集和位数交给校验器检查。
    "uscc": r"[0-9A-Za-z０-９Ａ-Ｚａ-ｚ][!-~０-９Ａ-Ｚａ-ｚ \t]*",
    "date": r"\d{4}\s*[年./-]\s*\d{1,2}\s*[月./-]\s*\d{1,2}\s*日?|\d{8}",
    "period": (
        r"\d{4}\s*[年./-]\s*\d{1,2}\s*[月./-]\s*\d{1,2}\s*日?\s*[-—至~]\s*"
        r"(?:\d{4}\s*[年./-]\s*\d{1,2}\s*[月./-]\s*\d{1,2}\s*日?|长期)"
    ),
}


def _value_pattern(fc: FieldConfig) -> str | None:
    # 已激活的旧模板可能仍显式使用此前内置的严格表达式。
    if fc.type == "uscc" and fc.pattern == _LEGACY_USCC_PATTERN:
        return _TYPE_PATTERNS["uscc"]
    if fc.pattern:
        return fc.pattern
    if fc.type == "business_period":
        return rf"(?:{_TYPE_PATTERNS['period']}|^长期$)"
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


def _lines_right_of(
    lines: list[OcrLine], anchor: OcrLine, *, allow_overlap: bool = False,
) -> list[OcrLine]:
    """与锚点同一视觉行、且在锚点右侧的行（证件"标签|值"左右并排版式），按 x 排序。"""
    anchor_left, _, anchor_right, _ = anchor.bbox
    overlap_limit = max(0.01, 0.25 * (anchor_right - anchor_left))
    right = [
        ln for ln in lines
        if ln is not anchor and _same_row(ln, anchor)
        and (ln.bbox[0] >= anchor_right - 0.01
             or (allow_overlap and ln.bbox[0] >= anchor_right - overlap_limit
                 and ln.cx > anchor_right))
    ]
    return sorted(right, key=lambda l: l.bbox[0])


def _crosses_label_rows(
    value: OcrLine, anchor: OcrLine, lines: list[OcrLine], anchors: set[str],
) -> bool:
    """水印形成的大框可能同时盖住相邻字段行，不能作为单行字段的值。"""
    x0, y0, x1, y1 = anchor.bbox
    if value.bbox[3] - value.bbox[1] <= 1.5 * (y1 - y0):
        return False
    column_tolerance = max(0.01, (x1 - x0) / 2)
    return any(
        label is not anchor and label.text.strip() in anchors
        and abs(label.bbox[0] - x0) <= column_tolerance
        and label.bbox[2] <= value.bbox[0] + 0.01
        and not _same_row(anchor, label) and _same_row(value, label)
        for label in lines
    )


def _before_other_anchor(text: str, anchors: set[str]) -> str:
    """取合并文本框内下一字段标签之前的值。"""
    end = min((pos for anchor in anchors if (pos := text.find(anchor)) >= 0),
              default=len(text))
    return text[:end].rstrip("：: ，,、\t ") if end < len(text) else text


def _union_bbox(bboxes: list[list[float]]) -> list[float]:
    if not bboxes:
        return [0.0, 0.0, 0.0, 0.0]
    return [
        min(b[0] for b in bboxes), min(b[1] for b in bboxes),
        max(b[2] for b in bboxes), max(b[3] for b in bboxes),
    ]


def _crop_text_box(text: str, bbox: list[float], start: int, end: int) -> list[float]:
    """缺少词坐标时，按字宽比例裁出同一行中的字段值。"""
    if start == 0 and end == len(text):
        return bbox
    widths = [0.33 if char.isspace() else (1.0 if east_asian_width(char) in ("W", "F") else 0.6)
              for char in text]
    left, top, right, bottom = bbox
    unit = (right - left) / sum(widths)
    return [left + unit * sum(widths[:start]), top, left + unit * sum(widths[:end]), bottom]


def _line_text_bbox(line: OcrLine, start: int, end: int) -> list[float]:
    words = [(a, b, box) for a, b, box in line.word_boxes if a < end and b > start]
    if not words or words[0][0] > start or words[-1][1] < end:
        return _crop_text_box(line.text, line.bbox, start, end)
    return _union_bbox([
        _crop_text_box(line.text[a:b], box, max(start, a) - a, min(end, b) - a)
        for a, b, box in words
    ])


def _value_bbox(value: str, lines: list[OcrLine], fc: FieldConfig,
                label_line: OcrLine | None = None, label_end: int = 0) -> list[float] | None:
    """定位字段值在原 OCR 文本中的字符，排除标签、空白和已清理的水印词。"""
    if not value or not lines:
        return None
    ignored = re.compile(r"\s+" + "".join(f"|{re.escape(word)}" for word in fc.stop_words if word))
    characters = []
    positions = []
    for index, line in enumerate(lines):
        offset = label_end if line is label_line else 0
        skipped = {pos for match in ignored.finditer(line.text, offset)
                   for pos in range(match.start(), match.end())}
        for pos in range(offset, len(line.text)):
            if pos not in skipped:
                characters.append(line.text[pos])
                positions.append((index, pos))
    target = re.sub(r"\s+", "", value)
    start = "".join(characters).find(target)
    selected = positions[start:start + len(target)] if start >= 0 else positions
    ranges: dict[int, tuple[int, int]] = {}
    for index, pos in selected:
        ranges[index] = (ranges.get(index, (pos, pos))[0], pos + 1)
    boxes = [_line_text_bbox(lines[index], first, end) for index, (first, end) in ranges.items()]
    return _union_bbox(boxes) if boxes else None


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
    fc: FieldConfig,
    all_anchor_texts: set[str],
    claimed: set[int],
) -> tuple[str, list[OcrLine]]:
    """单个字段取值。优先级：同行剩余 → 同行右侧 → 下方（多行字段继续吞行）。

    证件版式两类：标签与值同一行（"姓名张三"）、标签与值并排成两个检测行
    （OCR 输出"姓名"和"张三"两行、同一视觉行）——后者靠 _lines_right_of 命中。
    """
    anchor_line = lines[idx]
    rest = _strip_anchor(anchor_line.text, anchor_word, anchor_start).strip()
    right_lines = _lines_right_of(lines, anchor_line, allow_overlap=fc.type == "date")
    other_anchors = all_anchor_texts - set(fc.anchors)
    if not fc.multi_line:
        next_label = min(
            (line.bbox[0] for line in right_lines
             if any(line.text.strip().startswith(label) for label in other_anchors)),
            default=2.0,
        )
        right_lines = [line for line in right_lines
                       if line.bbox[0] <= next_label
                       and not _crosses_label_rows(line, anchor_line, lines, all_anchor_texts)]
    else:
        right_lines = [line for line in right_lines if id(line) not in claimed]
    right = [
        line for line in right_lines if not any(a in line.text for a in all_anchor_texts)
    ]
    right_text = "".join(l.text for l in right).strip()
    below = _line_below(lines, idx, anchor_line)
    if below is not None and (any(a in below.text for a in all_anchor_texts)
                              or fc.multi_line and id(below) in claimed):
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
            m = re.search(pat, source)
            if m:
                return m.group(0), used
        if fc.type == "business_period":
            # 同行已有文字但期限格式不完整：保留识别值，由校验器给出原因。
            rest = _before_other_anchor(rest, all_anchor_texts - set(fc.anchors))
            if rest:
                return rest, [anchor_line]
            if right_text:
                return right_text, right
        return "", []

    if not fc.multi_line:
        rest = _before_other_anchor(rest, other_anchors)
    if fc.type == "enum" and fc.allowed:
        if not rest and not V.validate_enum(_clean_value(right_text, fc), fc.allowed)[0]:
            for line in right_lines:
                value = _before_other_anchor(line.text, other_anchors)
                if value != line.text and V.validate_enum(_clean_value(value, fc), fc.allowed)[0]:
                    return value, [line]

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


def _assemble_fields(
    lines: list[OcrLine], cfg: CertConfig,
) -> tuple[list[FieldResult], dict[str, list[float] | None]]:
    """按 YAML 配置从 OCR 行组装全部字段。两遍策略：

    第一遍处理单行字段并"认领"其值行（claimed），第二遍处理多行字段——
    多行吸收时跳过已被认领的行，避免把别的字段的值吞进来。
    """
    # Large watermark boxes can overlap several visual rows. Ignore standalone
    # configured noise before spatial matching, not only after value extraction.
    noise_words = {word for field in cfg.fields for word in field.stop_words}
    # 空文本行（检测器在空白处误检的框：识别为空、置信度 0）同样在空间匹配前剔除——
    # 它不贡献文字，却会把字段置信度拉到 0、把 bbox 撑宽
    lines = [line for line in lines
             if line.text.strip() and line.text.strip() not in noise_words]
    all_anchor_texts = {a for f in cfg.fields for a in f.anchors}
    claimed: set[int] = set()
    extracted: dict[str, tuple[str, list[OcrLine], OcrLine | None, int]] = {}

    def run(fc) -> None:
        hit = _find_anchor_line(lines, fc.anchors, all_anchor_texts, exact_only=fc.optional)
        if hit is None:
            if not fc.optional:
                extracted[fc.name] = ("", [], None, 0)
            return
        i, anchor_word, start = hit
        value, used = _extract_field(
            lines, i, anchor_word, start, fc, all_anchor_texts, claimed
        )
        extracted[fc.name] = (value, used, lines[i], start + len(anchor_word))

    # 第一遍：单行字段（先认领）
    for fc in cfg.fields:
        if not fc.multi_line:
            run(fc)
            if fc.name in extracted:
                claimed.update(id(line) for line in extracted[fc.name][1])
    # 第二遍：多行字段（吸收时避开已认领行）
    for fc in cfg.fields:
        if fc.multi_line:
            run(fc)

    results: list[FieldResult] = []
    value_boxes: dict[str, list[float] | None] = {}
    for fc in cfg.fields:
        if fc.name not in extracted:
            continue
        raw_value, used_lines, label_line, label_end = extracted[fc.name]
        value = _clean_value(raw_value, fc)
        conf = min((l.confidence for l in used_lines), default=0.0)
        value_boxes[fc.name] = _value_bbox(value, used_lines, fc, label_line, label_end)
        results.append(FieldResult(
            name=fc.name,
            value=value,
            confidence=round(conf, 4),
            bbox=_union_bbox([line.bbox for line in used_lines]) if used_lines else None,
        ))
    return results, value_boxes


def assemble_fields(lines: list[OcrLine], cfg: CertConfig) -> list[FieldResult]:
    """对外提供字段值及其展示坐标。"""
    results, value_boxes = _assemble_fields(lines, cfg)
    for result in results:
        result.bbox = value_boxes[result.name]
    return results


def validate_fields(fields: list[FieldResult], cfg: CertConfig) -> None:
    """就地执行字段级校验 + 交叉校验，写回 validation / needs_review。"""
    by_name = {f.name: f for f in fields}
    raw_values = {f.name: f.value for f in fields}

    # 字段类型校验
    for fr in fields:
        fr.validation = FieldValidation()
    for fc in cfg.fields:
        fr = by_name.get(fc.name)
        if fr is None or not fr.value:
            continue
        if fc.type == "enum" and fc.allowed:
            ok, errs = V.validate_enum(fr.value, fc.allowed)
        elif cfg.type_id == "license" and fc.name == "经营范围":
            ok, errs = V.validate_business_scope(fr.value)
        elif cfg.type_id == "id_front" and fc.name == "民族":
            ok = re.fullmatch(r"[\u3400-\u9fff]{1,8}", fr.value) is not None
            errs = [] if ok else ["民族栏混入其他文字，请核对原图"]
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
        fr.needs_review = (not fr.value or not fr.validation.passed
                           or fr.confidence < CONFIDENCE_REVIEW_THRESHOLD)


def _recheck_bbox(
    field: FieldResult, fc: FieldConfig, labels: dict[str, OcrLine],
    peers: list[tuple[FieldConfig, FieldResult]],
) -> list[float] | None:
    """根据已有值框或标签行划定复检区，避免裁入其他字段。"""
    if field.bbox:
        bbox = field.bbox
    else:
        label = labels.get(field.name)
        if label is None:
            return None
        lx0, ly0, lx1, ly1 = label.bbox
        height = ly1 - ly0
        char_width = (lx1 - lx0) / max(len(label.text.strip()), 1)
        if height <= 0 or char_width <= 0:
            return None
        other_labels = [line for line in labels.values() if line is not label]
        if not other_labels:
            return None
        below = min((line.bbox[1] for line in other_labels if line.bbox[1] >= ly1),
                    default=1.0)
        above = max((line.bbox[3] for line in other_labels if line.bbox[3] <= ly0),
                    default=0.0)
        right = min((line.bbox[0] - 0.25 * char_width for line in
                     _lines_right_of(other_labels, label)), default=1.0)
        y0 = max(ly0 - 0.2 * height, (above + ly0) / 2)
        y1 = min(ly1 + 0.2 * height, (ly1 + below) / 2)
        columns = []
        for peer_cfg, peer in peers:
            peer_label = labels.get(peer.name)
            if (peer_label is not None and peer.bbox and not peer.needs_review
                    and abs(peer_label.bbox[0] - lx0) <= char_width * 2
                    and peer.bbox[0] >= max(lx1, peer_label.bbox[2]) - 0.01
                    and _same_row(peer_label, OcrLine("", peer.bbox))):
                columns.append((peer_cfg, peer.bbox))

        if fc.position == "next_line" and not columns:
            # 紧邻下行的值与标签左对齐，下一字段标签是裁剪上限。
            bbox = [max(0.0, lx0 - 0.4 * char_width), ly1,
                    right, min(below, ly1 + (3.5 if fc.multi_line else 1.5) * height)]
        else:
            left = max(lx1, min((box[0] - 0.5 * char_width for _, box in columns),
                                default=lx1))
            if not fc.multi_line and _value_pattern(fc):
                similar = [box[2] + 1.25 * char_width for spec, box in columns
                           if spec.type == fc.type and _value_pattern(spec)]
                if similar:
                    right = min(right, max(similar))
            if fc.multi_line:
                y1 = min(below, ly1 + 3 * height)
            bbox = [left, y0, right, y1]

    x0, y0, x1, y1 = bbox
    if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
        return None
    for _, peer in peers:
        if not peer.bbox or peer.needs_review:
            continue
        px0, py0, px1, py1 = peer.bbox
        overlap = (max(0.0, min(x1, px1) - max(x0, px0))
                   * max(0.0, min(y1, py1) - max(y0, py0)))
        if overlap > 0.15 * (px1 - px0) * (py1 - py0):
            return None
    return bbox


def _recheck_value(
    lines: list[OcrLine], fc: FieldConfig, other_anchors: set[str],
) -> tuple[FieldResult, list[float] | None] | None:
    used = [line for line in lines if _clean_value(line.text, fc)]
    used.sort(key=lambda line: (round(line.bbox[1], 2), line.bbox[0]))
    if not used:
        return None
    text = "".join(line.text.strip() for line in used)
    label_line = None
    label_end = 0
    for anchor in sorted(fc.anchors, key=len, reverse=True):
        if text.startswith(anchor):
            text = _strip_anchor(text, anchor)
            if used[0].text.strip().startswith(anchor):
                label_line = used[0]
                label_end = label_line.text.index(anchor) + len(anchor)
            break
    value = _clean_value(text, fc)
    if any(anchor in value for anchor in other_anchors):
        return None
    pattern = _value_pattern(fc)
    if pattern:
        matches = [match.group(0) for match in re.finditer(pattern, value)]
        value = matches[0] if len(matches) == 1 else ""
    confidence = min(line.confidence for line in used)
    if not value or not CONFIDENCE_REVIEW_THRESHOLD <= confidence <= 1.0:
        return None
    field = FieldResult(name=fc.name, value=value, confidence=round(confidence, 4),
                        bbox=_union_bbox([line.bbox for line in used]))
    return field, _value_bbox(value, used, fc, label_line, label_end)


def _same_value(left: str, right: str, fc: FieldConfig) -> bool:
    if fc.type == "date":
        date = V.parse_date_cn(left)
        if date is not None:
            return date == V.parse_date_cn(right)
    return re.sub(r"\s+", "", left) == re.sub(r"\s+", "", right)


def _scope_punctuation_review(field: FieldResult, cfg: CertConfig) -> bool:
    return (cfg.type_id == "license" and field.name == "经营范围" and bool(field.value)
            and not V.validate_business_scope(field.value)[0])


_SCOPE_SEPARATOR = re.compile(r"(?<=[\u3400-\u9fff])[，,；;：:。\.](?=[\u3400-\u9fff])")


def _recheck_fields(
    fields: list[FieldResult], lines: list[OcrLine], cfg: CertConfig,
    recognize_region: RegionRecognizer, value_boxes: dict[str, list[float] | None],
) -> list[FieldResult]:
    configs = {fc.name: fc for fc in cfg.fields}
    all_anchors = {anchor for fc in cfg.fields for anchor in fc.anchors}
    labels = {}
    for fc in cfg.fields:
        for line in lines:
            text = line.text.strip()
            if (line.confidence >= CONFIDENCE_REVIEW_THRESHOLD and
                    any(text.startswith(a) and not _clean_value(_strip_anchor(text, a), fc)
                        for a in fc.anchors)):
                labels[fc.name] = line
                break

    # 先复检证号，供同一张证件中待复检的出生日期和性别交叉校验。
    order = sorted(range(len(fields)), key=lambda i: configs[fields[i].name].type != "chinese_id")
    for i in order:
        field = fields[i]
        check_punctuation = _scope_punctuation_review(field, cfg)
        if field.confidence != 0 and not check_punctuation:
            continue
        fc = configs[field.name]
        peers = [(configs[peer.name], peer) for peer in fields if peer.name != field.name]
        bbox = _recheck_bbox(field, fc, labels, peers)
        if bbox is None:
            continue
        try:
            region_lines = recognize_region(bbox, fc.multi_line)
        except Exception:
            log.warning("字段 %s 局部复检失败", field.name, exc_info=True)
            continue
        recognized = _recheck_value(region_lines, fc, all_anchors - set(fc.anchors))
        if recognized is None:
            continue
        candidate, value_box = recognized
        if field.value and not _same_value(field.value, candidate.value, fc):
            if not check_punctuation or (
                _SCOPE_SEPARATOR.sub("", re.sub(r"\s+", "", field.value))
                != _SCOPE_SEPARATOR.sub("", re.sub(r"\s+", "", candidate.value))
            ):
                continue
        required_ids = {
            check.id_field for check in cfg.cross_checks
            if (check.kind == "idcard_birth_match" and check.date_field == field.name)
            or (check.kind == "idcard_gender_match" and check.gender_field == field.name)
        }
        verified_ids = {peer.name for _, peer in peers if peer.value and not peer.needs_review}
        if not required_ids.issubset(verified_ids):
            continue
        trial = [peer.model_copy() for peer in fields]
        trial[i] = candidate
        validate_fields(trial, cfg)
        if candidate.needs_review or any(
            set(after.validation.errors) - set(before.validation.errors)
            for before, after in zip(fields, trial)
        ):
            continue
        fields = trial
        value_boxes[field.name] = value_box
    return fields


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
    recognize_region: RegionRecognizer | None = None,
) -> CardResult:
    """从已有 OCR 行组装 CardResult（分类 → 组装 → 校验）。

    doc_type 已知时直接传入。recognize_region 可用于零置信度字段局部复检。
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
    fields, value_boxes = _assemble_fields(lines, cfg)
    validate_fields(fields, cfg)
    if recognize_region is not None and any(
        f.confidence == 0 or _scope_punctuation_review(f, cfg) for f in fields
    ):
        fields = _recheck_fields(fields, lines, cfg, recognize_region, value_boxes)
    # 复检使用检测框定位字段行，完成后换成仅用于显示的字段值坐标。
    for field in fields:
        field.bbox = value_boxes[field.name]
    issues = []
    for field in fields:
        if not field.needs_review:
            continue
        if not field.value:
            reason = "未提取到值"
        elif not field.validation.passed:
            reason = f"校验失败（{'；'.join(field.validation.errors)}）"
        else:
            reason = f"置信度低（{field.confidence}），建议人工复核"
        issues.append(f"{field.name}：{reason}")

    return CardResult(
        file_sha256=digest, filename=path.name,
        doc_type=doc_type, doc_type_cn=cfg.name, type_confidence=conf,
        fields=fields, issues=issues,
        duration_ms=int((time.perf_counter() - t0) * 1000),
    )

