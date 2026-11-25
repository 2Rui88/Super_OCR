"""字段校验器 —— 纯规则代码，OCR 识别结果的安全网。

设计原则：
- 每个校验器返回 (passed, errors)，errors 是"人话"原因，直接进结果 JSON；
- 匹配前先归一化（去空格/换行）——OCR 常把 18 位证号断成两行（leaudit 踩过的坑）；
- 校验不过的字段由上层标 needs_review，保留 OCR 置信度。
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------- 身份证号

_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK = "10X98765432"


def normalize_id_number(raw: str) -> str:
    """归一化证号：去空白、统一大写 X（容忍 OCR 断行/断空格）。"""
    return re.sub(r"\s+", "", str(raw or "")).upper()


def validate_chinese_id(raw: str) -> tuple[bool, list[str]]:
    id_no = normalize_id_number(raw)
    if len(id_no) != 18:
        return False, [f"身份证号应为18位，识别为{len(id_no)}位"]
    if not re.fullmatch(r"[0-9]{17}[0-9X]", id_no):
        return False, ["身份证号前17位应全为数字，且末位为数字或X"]
    # 日期段合法性
    ok, err = _check_date_segment(id_no[6:14])
    if not ok:
        return False, [f"证号中出生日期段无效：{err}"]
    weighted = sum(int(digit) * weight for digit, weight in zip(id_no[:17], _ID_WEIGHTS))
    expected = _ID_CHECK[weighted % 11]
    if id_no[-1] != expected:
        return False, [f"校验位错误，期望 {expected}，识别为 {id_no[-1]}"]
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
    errors = []
    if len(code) != 18:
        errors.append(f"统一社会信用代码应为18位，识别为{len(code)}位")
    invalid = sorted(set(code) - set(_USCC_CHARS))
    if invalid:
        errors.append(f"含非法字符 {'、'.join(invalid)}")
    if errors:
        return False, errors
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


def validate_business_period(raw: str) -> tuple[bool, list[str]]:
    """执照可以只标注长期，有日期区间时核查实际日期和起止顺序。"""
    if re.sub(r"\s+", "", str(raw or "")) == "长期":
        return True, []
    return validate_period(raw)


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


def validate_business_scope(raw: str) -> tuple[bool, list[str]]:
    text = re.sub(r"\s+", "", raw)
    # 多个类目之间连用冒号、或句号把审批条件截开时，优先复核原图。
    separators = re.findall(r"(?<=[\u3400-\u9fff])[：:](?=[\u3400-\u9fff])", text)
    if len(separators) > 1 or re.search(r"批准的项目[。.]经相关部门", text):
        return False, ["经营范围的分隔标点可能识别有误，请核对原图"]
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
    "business_period": validate_business_period,
}


def run_validator(type_name: str, value: str) -> tuple[bool, list[str]]:
    fn = FIELD_VALIDATORS.get(type_name)
    if fn is None or not str(value or "").strip():
        return True, []  # 未注册类型/空值跳过（空值由 required 逻辑处理）
    return fn(value)
