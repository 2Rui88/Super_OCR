"""证件配置加载 + 类型分类。

- 启动时扫描 config/certs/*.yaml，注册进内存表（新增证件 = 加文件，不改代码）；
- 分类结合证件特征和关键词得分，特征不足或分数接近时判 unknown。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from app.certs import validators


class ClassificationConfig(BaseModel):
    required: list[str] = Field(default_factory=list)
    positive: list[str] = Field(default_factory=list)
    negative: list[str] = Field(default_factory=list)


class FieldConfig(BaseModel):
    name: str
    anchors: list[str] = Field(default_factory=list)
    type: str = "text"              # 引用 validators.FIELD_VALIDATORS
    pattern: str | None = None      # 值的正则（可选）
    allowed: list[str] = Field(default_factory=list)  # enum 用
    position: str = "same_line"     # same_line | next_line
    multi_line: bool = False
    optional: bool = False          # 未识别到字段标签时不输出
    # 值清洗：从值中剔除的水印/噪声词（样例件的"样本"戳、复印件"副本"字样等）
    stop_words: list[str] = Field(default_factory=lambda: ["样本"])


class CrossCheckConfig(BaseModel):
    kind: str                       # idcard_birth_match / idcard_gender_match / period_order / date_order
    field: str | None = None
    id_field: str | None = None
    date_field: str | None = None
    gender_field: str | None = None
    early: str | None = None
    late: str | None = None


class CertConfig(BaseModel):
    type_id: str
    name: str
    classification: ClassificationConfig = Field(default_factory=ClassificationConfig)
    fields: list[FieldConfig] = Field(default_factory=list)
    cross_checks: list[CrossCheckConfig] = Field(default_factory=list)


def load_certs(config_dir: str | Path) -> dict[str, CertConfig]:
    """扫描目录注册全部证件配置。文件名即 type_id 的约定来源。"""
    registry: dict[str, CertConfig] = {}
    for path in sorted(Path(config_dir).glob("*.yaml")):
        with open(path, encoding="utf-8") as f:
            cfg = CertConfig.model_validate(yaml.safe_load(f))
        registry[cfg.type_id] = cfg
    return registry


def validate_cert_text(yaml_text: str) -> CertConfig:
    """单份 YAML 文本 → 严格校验后的配置。语法/结构/正则/校验器引用全查。

    可插拔编辑入口共用的守门函数：这里抛出的异常信息会原样回到编辑界面。
    """
    data = yaml.safe_load(yaml_text)
    if not isinstance(data, dict):
        raise ValueError("YAML 顶层必须是映射（键值对），当前是 "
                         f"{type(data).__name__}")
    cfg = CertConfig.model_validate(data)
    if not cfg.fields:
        raise ValueError("fields 不能为空：至少定义一个字段")
    legal_types = {"text", "enum", *validators.FIELD_VALIDATORS}
    for field in cfg.fields:
        if not field.anchors:
            raise ValueError(f"字段「{field.name}」缺少 anchors（至少一个锚点词）")
        if field.pattern:
            import re

            try:
                re.compile(field.pattern)
            except re.error as exc:
                raise ValueError(f"字段「{field.name}」的 pattern 不是合法正则：{exc}") from exc
        if field.type not in legal_types:
            raise ValueError(f"字段「{field.name}」的 type「{field.type}」未注册"
                             f"（可用：{sorted(legal_types)}）")
        if field.type == "enum" and not field.allowed:
            raise ValueError(f"字段「{field.name}」是 enum，必须给出 allowed 候选列表")
    for check in cfg.cross_checks:
        if check.kind not in ("idcard_birth_match", "idcard_gender_match",
                              "period_order", "date_order"):
            raise ValueError(f"cross_checks 里未知的 kind：{check.kind}")
    return cfg


def load_certs_from_texts(texts: dict[str, str]) -> dict[str, CertConfig]:
    """{type_id: yaml 文本} → registry。试跑快照 / DB 激活集构建入口。"""
    registry: dict[str, CertConfig] = {}
    for type_id, text in texts.items():
        cfg = validate_cert_text(text)
        if cfg.type_id != type_id:
            raise ValueError(f"YAML 内的 type_id「{cfg.type_id}」与版本归属「{type_id}」不一致")
        registry[cfg.type_id] = cfg
    return registry


def registry_hash(registry: dict[str, CertConfig]) -> str:
    """registry 内容指纹：进证件缓存键，YAML 变更 → 缓存自动失效。"""
    payload = {type_id: cfg.model_dump(mode="json")
               for type_id, cfg in sorted(registry.items())}
    # 未声明可选字段的旧版配置继续使用原有指纹，显式启用时才改变指纹。
    for cfg in payload.values():
        for field in cfg["fields"]:
            if not field["optional"]:
                del field["optional"]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


# ---------------------------------------------------------------- 分类

_REQUIRED_BONUS = 5.0     # 强特征命中加分
_POSITIVE_EACH = 1.0      # 正向词每个 +1（同一词最多计 3 次）
_NEGATIVE_EACH = -2.0     # 负向词每个 -2
_MIN_SCORE = 2.0          # 低于此分视为"不是这4类证件"
_MIN_MARGIN = 1.0         # 与第二名的最小领先幅度

_OTHER_ID_TITLE = re.compile(
    r".*(?:工作证|工牌|居住证|员工(?:入职|信息)登记表|入职登记表)(?:[（(][^()（）]*[）)])?"
)
_ID_FRONT_LABELS = ("姓名", "性别", "民族", "出生", "住址")


def _has_id_front_evidence(text: str) -> bool:
    text = "\n".join(re.sub(r"\s+", "", line) for line in text.splitlines())
    if any(_OTHER_ID_TITLE.fullmatch(line) for line in text.splitlines()):
        return False
    labels = sum(label in text for label in _ID_FRONT_LABELS)
    number_label = re.search(r"公民身份号码|身份[证]?号码", text)
    number = re.search(r"(?<![0-9])[0-9]{17}[0-9Xx](?![0-9])", text)
    # 证号标签和一组基本字段是正面的典型组合。标签漏读时仍可用证号或完整字段组判断。
    return (number_label is not None and (labels >= 2 or "居民身份证" in text)
            or labels >= 3 and number is not None
            or labels == len(_ID_FRONT_LABELS))


def classify(
    text: str, registry: dict[str, CertConfig]
) -> tuple[str, float]:
    """全文关键词评分 → (type_id, confidence)。判不出返回 ('unknown', 0)。"""
    scores: dict[str, float] = {}
    for type_id, cfg in registry.items():
        if type_id == "id_front" and not _has_id_front_evidence(text):
            continue
        cl = cfg.classification
        score = 0.0
        for kw in cl.required:
            if kw in text:
                score += _REQUIRED_BONUS
        for kw in cl.positive:
            score += _POSITIVE_EACH * min(3, text.count(kw))
        for kw in cl.negative:
            if kw in text:
                score += _NEGATIVE_EACH
        scores[type_id] = score

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    if not ranked or ranked[0][1] < _MIN_SCORE:
        return "unknown", 0.0
    top_type, top_score = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    if top_score - second < _MIN_MARGIN:
        return "unknown", 0.5   # 分不清，宁可拒识
    confidence = round(top_score / (top_score + second), 2) if second > 0 else 1.0
    return top_type, confidence


# ---------------------------------------------------------------- 校验执行

def run_cross_check(
    check: CrossCheckConfig, values: dict[str, str]
) -> tuple[str, list[str]]:
    """执行一条交叉校验，返回 (字段名, 错误列表)。字段名为报错归属。"""
    k = check.kind
    if k == "idcard_birth_match":
        id_no, birth = values.get(check.id_field, ""), values.get(check.date_field, "")
        if id_no and birth:
            expect = validators.idcard_birth(id_no)
            actual = validators.parse_date_cn(birth)
            if expect and actual and expect != actual:
                return check.date_field, [f"出生日期({actual})与身份证号隐含的({expect})不一致"]
    elif k == "idcard_gender_match":
        id_no, gender = values.get(check.id_field, ""), values.get(check.gender_field, "")
        if id_no and gender:
            expect = validators.idcard_gender(id_no)
            g = gender.strip()
            if expect and g in ("男", "女") and expect != g:
                return check.gender_field, [f"性别({g})与身份证号第17位隐含的({expect})不一致"]
    elif k == "period_order":
        raw = values.get(check.field or "", "")
        if raw:
            start, end = validators.split_period(raw)
            if start is not None:
                ok, errs = validators.validate_date_range(start, end or "")
                if not ok:
                    return check.field or "", errs
    elif k == "date_order":
        early = validators.parse_date_cn(values.get(check.early or "", ""))
        late_raw = values.get(check.late or "", "")
        late_m: str | None = None
        if late_raw:
            start, _ = validators.split_period(late_raw)
            late_m = validators.parse_date_cn(start or late_raw)
        if early and late_m and early > late_m:
            return check.early or "", [
                f"{check.early}({early})晚于{check.late}的起始({late_m})，顺序有误"
            ]
    return "", []
