"""工具注册表（内核机制）—— 业务工具经 register() 注入，本模块零业务依赖。

ToolSpec 同时携带 LLM 侧元数据（描述/参数 Schema/能力文案）与执行侧
handler；tool_definitions() 与提示词能力清单由同一张表生成，天然同步。
LLM 给出的参数一律经 .arguments() 钳制后才可使用。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict                      # {参数名: {type/minimum/maximum/default/maxLength/description}}
    handler: Callable[..., Awaitable[dict]]
    ability: str                          # 提示词能力清单中的一行

    def arguments(self, raw: dict | None) -> dict:
        """双向钳制：null/越界/类型不符的就地修正为默认值，字符串清洗截断。

        未提供且无默认值的参数直接省略（不产出 null 值）——历史里回放的
        参数就是模型下一轮模仿的模板，带 null 会诱导它输出 Python 的 None。
        """
        output = {}
        raw = raw or {}
        for name, schema in self.parameters.items():
            value = raw.get(name)
            if schema.get("type") == "integer":
                if type(value) is not int:
                    value = schema.get("default")
                if value is not None:
                    value = max(schema["minimum"], min(schema["maximum"], value))
            else:
                value = value.strip()[:schema.get("maxLength", 255)] \
                    if isinstance(value, str) else ""
            if value is not None:
                output[name] = value
        return output


TOOLS: dict[str, ToolSpec] = {}


def register(*specs: ToolSpec) -> None:
    for spec in specs:
        TOOLS[spec.name] = spec


def reset() -> None:
    """仅测试用：清空注册表。"""
    TOOLS.clear()


def tool_definitions() -> list[dict]:
    return [
        {"type": "function", "function": {
            "name": spec.name, "description": spec.description,
            "parameters": {"type": "object", "properties": spec.parameters,
                           "additionalProperties": False},
        }}
        for spec in TOOLS.values()
    ]


def abilities_section() -> str:
    return "\n".join(f"- {spec.name}：{spec.ability}" for spec in TOOLS.values())
