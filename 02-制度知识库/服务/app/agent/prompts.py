"""系统提示词组装器 —— 框架段(内核) + 业务注入段 + 能力清单(注册表自动生成)。

内核不含业务规则，由业务包注册查阅、拒答和直答提示。
"""
from __future__ import annotations

from app.agent import registry

# 框架段：循环预算与工具使用规范（业务无关，内核自带）
FRAMEWORK_RULES = (
    "你通过调用工具查阅资料，再依据资料回答用户问题。"
    "每次决定要么调用工具继续查阅，要么输出「查阅完成」结束查阅阶段。"
    "工具参数必须完整、明确；对同一信息的重复查询没有意义。"
)

_sections: list[str] = []
_refusal: str | None = None
_direct_reply: str | None = None


def register_section(text: str) -> None:
    """业务包注册一段提示词（按注册顺序拼接）。"""
    if text not in _sections:
        _sections.append(text)


def register_refusal(phrase: str) -> None:
    """业务包注册拒答话术；引擎据此在流式输出中识别并转成 refuse 事件。"""
    global _refusal
    _refusal = phrase


def refusal() -> str | None:
    return _refusal


def register_direct_reply(text: str) -> None:
    """注册零工具调用时用于生成正式回复的提示。"""
    global _direct_reply
    _direct_reply = text


def direct_reply_prompt() -> str:
    return _direct_reply or "你是对话助手。请自然、简洁地回答用户。"


def reset() -> None:
    """仅测试用：清空注入状态。"""
    global _refusal, _direct_reply
    _sections.clear()
    _refusal = None
    _direct_reply = None


def system_prompt() -> str:
    parts = [FRAMEWORK_RULES, *_sections]
    abilities = registry.abilities_section()
    if abilities:
        parts.append("可用工具：\n" + abilities)
    return "\n".join(parts)
