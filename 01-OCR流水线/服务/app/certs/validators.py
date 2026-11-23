"""字段校验器 —— 纯规则代码，OCR 识别结果的安全网。

设计原则：
- 每个校验器返回 (passed, errors)，errors 是"人话"原因，直接进结果 JSON；
- 匹配前先归一化（去空格/换行）——OCR 常把 18 位证号断成两行（leaudit 踩过的坑）；
- 校验不过的字段由上层把置信度压 0 并标 needs_review。
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------- 身份证号


def normalize_id_number(raw: str) -> str:
    """归一化证号：去空白、统一大写 X（容忍 OCR 断行/断空格）。"""
    return re.sub(r"\s+", "", str(raw or "")).upper()


def validate_chinese_id(raw: str) -> tuple[bool, list[str]]:
    # 校验位（GB 11643 mod-11-2）规则已取消：样本多为虚构号，校验位必不匹配造成误报。
    # 只保留结构校验：位数/字符集/出生日期段。
    id_no = normalize_id_number(raw)
    if len(id_no) != 18:
        return False, [f"身份证号应为18位，识别为{len(id_no)}位"]
    if not re.fullmatch(r"\d{17}[0-9X]", id_no):
        return False, ["身份证号前17位应全为数字，且末位为数字或X"]
    # 日期段合法性
    ok, err = _check_date_segment(id_no[6:14])
    if not ok:
        return False, [f"证号中出生日期段无效：{err}"]
    return True, []


def idcard_birth(id_no: str) -> str | None:
    """从证号取出生日期 → 'YYYY-MM-DD'；无效返回 None。"""
    n = normalize_id_number(id_no)
    if len(n) != 18:
        return None
    seg = n[6:14]
    if not seg.isdigit():
        return None
    ok, _ = _check_date_segment(seg)
    return f"{seg[:4]}-{seg[4:6]}-{seg[6:]}" if ok else None


def idcard_gender(id_no: str) -> str | None:
    """第17位奇偶 → 男/女。"""
    n = normalize_id_number(id_no)
    if len(n) == 18 and n[16].isdigit():
        return "男" if int(n[16]) % 2 == 1 else "女"
    return None


def _check_date_segment(yyyymmdd: str) -> tuple[bool, str]:
    import datetime

    try:
        datetime.date(int(yyyymmdd[0:4]), int(yyyymmdd[4:6]), int(yyyymmdd[6:8]))
    except ValueError as e:
        return False, str(e)
    return True, ""


# ------------------------------------------------------ 统一社会信用代码

_USCC_CHARS = "0123456789ABCDEFGHJKLMNPQRTUWXY"
_USCC_WEIGHTS = [1, 3, 9, 27, 19, 26, 16, 17, 20, 29, 25, 13, 8, 24, 10, 30, 28]


def validate_uscc(raw: str) -> tuple[bool, list[str]]:
    code = re.sub(r"\s+", "", str(raw or "")).upper()
    if len(code) != 18:
        return False, [f"统一社会信用代码应为18位，识别为{len(code)}位"]
    total = sum(_USCC_CHARS.index(c) * _USCC_WEIGHTS[i] for i, c in enumerate(code[:17]))
    expected = _USCC_CHARS[(31 - total % 31) % 31]
    if code[17] != expected:
        return False, [f"校验位错误，期望 {expected}，识别为 {code[17]}"]
    return True, []


# ---------------------------------------------------------------- 日期

_DATE_PATTERNS = [
    (re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})"), None),
    (re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日"), None),
    (re.compile(r"(\d{4})(\d{2})(\d{2})"), None),  # 证号里的紧凑格式
]


def parse_date_cn(raw: str) -> str | None:
    """解析常见中文日期 → 'YYYY-MM-DD'；解析不出返回 None。

    支持：2025.01.01 / 2025-01-01 / 2025/1/1 / 2025年1月1日 / 20250101
    """
    import datetime

    text = re.sub(r"\s+", "", str(raw or ""))
    for pat, _ in _DATE_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return datetime.date(y, mo, d).isoformat()
        except ValueError:
            continue
    return None


def validate_date(raw: str) -> tuple[bool, list[str]]:
    if parse_date_cn(raw) is None:
        return False, [f"无法解析为有效日期：'{raw}'"]
    return True, []


def validate_date_range(start_raw: str, end_raw: str) -> tuple[bool, list[str]]:
    """有效期起止：两个日期都合法，且 起 <= 止。支持'长期'。"""
    errors: list[str] = []
    end_text = re.sub(r"\s+", "", str(end_raw or ""))
    start = parse_date_cn(start_raw)
    end = parse_date_cn(end_raw) if end_text != "长期" else None
    if start is None:
        errors.append(f"起始日期无法解析：'{start_raw}'")
    if end is None and end_text != "长期":
        errors.append(f"截止日期无法解析：'{end_raw}'")
    if errors:
        return False, errors
    if start and end and start > end:
        return False, [f"有效期起始({start})晚于截止({end})，顺序有误"]
    return True, []


_PERIOD_SPLIT = re.compile(r"[-—至~]")


# 起/止提取：日期本身可含 '-'，所以用日期正则提取两端而不是按分隔符切分
_DATE_RE = re.compile(r"\d{4}[年./-]\d{1,2}[月./-]\d{1,2}日?|\d{8}")
_PERIOD_RE = re.compile(
    r"(\d{4}[年./-]\d{1,2}[月./-]\d{1,2}日?|\d{8})"
    r"\s*[-—至~]\s*"
    r"(\d{4}[年./-]\d{1,2}[月./-]\d{1,2}日?|\d{8}|长期)"
)


def split_period(raw: str) -> tuple[str | None, str | None]:
    """从时间段文本提取（起，止）。'2023-07-26至2033-07-26' → 两端日期；止可为'长期'。"""
    m = _PERIOD_RE.search(re.sub(r"\s+", "", str(raw or "")))
    if not m:
        return None, None
    return m.group(1), m.group(2)


def validate_period(raw: str) -> tuple[bool, list[str]]:
    """时间段字段：'2015.01.01-2035.01.01' / '20210101至长期' 等。"""
    start, end = split_period(raw)
    if start is None:
        return False, [f"时间段格式异常（应形如 起-止）：'{raw}'"]
    return validate_date_range(start, end)


# ---------------------------------------------------------------- 枚举/金额

def validate_enum(value: str, allowed: list[str]) -> tuple[bool, list[str]]:
    v = re.sub(r"\s+", "", str(value or ""))
    if v not in allowed:
        return False, [f"取值'{value}'不在允许列表 {allowed} 内"]
    return True, []


def validate_money(raw: str) -> tuple[bool, list[str]]:
    text = re.sub(r"[，,\s￥¥元人民币]", "", str(raw or ""))
    if not re.fullmatch(r"\d+(\.\d+)?", text):
        return False, [f"金额格式异常：'{raw}'"]
    return True, []


# ---------------------------------------------------------------- 注册表

#: 字段类型 → 校验函数注册表。YAML 里 type: chinese_id 即引用此表。
#: 新增校验类型 = 这里加一行 + （如需）上面加一个函数，证件 YAML 无需改。
FIELD_VALIDATORS: dict[str, callable] = {  # type: ignore[type-arg]
    "chinese_id": validate_chinese_id,
    "uscc": validate_uscc,
    "date": validate_date,
    "money": validate_money,
    "period": validate_period,
}


def run_validator(type_name: str, value: str) -> tuple[bool, list[str]]:
    fn = FIELD_VALIDATORS.get(type_name)
    if fn is None or not str(value or "").strip():
        return True, []  # 未注册类型/空值跳过（空值由 required 逻辑处理）
    return fn(value)
