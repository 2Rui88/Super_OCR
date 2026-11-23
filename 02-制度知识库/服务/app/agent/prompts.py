"""系统提示词组装器 —— 框架段(内核) + 业务注入段 + 能力清单(注册表自动生成)。

内核不含任何业务措辞；业务包(agent_business)import 时调 register_section /
register_refusal 完成注入。未注入任何业务段时引擎退化为通用对话。
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


def reset() -> None:
    """仅测试用：清空注入状态。"""
    _sections.clear()
    global _refusal
    _refusal = None


def system_prompt() -> str:
    parts = [FRAMEWORK_RULES, *_sections]
    abilities = registry.abilities_section()
    if abilities:
        parts.append("可用工具：\n" + abilities)
    return "\n".join(parts)
