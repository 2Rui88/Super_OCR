"""上下文组装器（内核）—— 轮分组 → 历史瘦身 → 预算截断 → 边界保护。

三视图原则：落库是全量真相；本模块产出的只是喂给模型的瘦身视图。
轮 = 一条 user 消息到下一条 user 消息之前；不完整的轮（终答缺失或工具
调用悬空）整轮丢弃，天然保证不会把半轮喂给 API。
"""
from __future__ import annotations

import json
import re

from app.agent import prompts
from app.config import settings


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _field(message: dict, name: str, default):
    value = message.get(name)
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return value if value is not None else default


def _compact_turn(turn: list[dict], *, direct_reply: bool) -> list[dict]:
    """一轮 → 两条消息；直答历史省略依据和查阅轨迹。"""
    last = turn[-1]
    if (last.get("role") != "assistant" or last.get("incomplete")
            or not last.get("content") or _field(last, "tool_calls", [])):
        return []
    calls: dict[str, dict] = {}
    summaries: list[str] = []
    for message in turn[1:-1]:
        for call in _field(message, "tool_calls", []):
            if call["id"] in calls:
                return []
            calls[call["id"]] = call["function"]
        if message.get("role") == "tool":
            function = calls.pop(message.get("tool_call_id"), None)
            if function is None:
                return []
            if not direct_reply:
                args = _field(function, "arguments", {})
                result = _field(message, "content", {})
                target = args.get("query") or args.get("chunk_id") or ""
                count = result.get("count") if isinstance(result, dict) else None
                outcome = f"{count}块" if count is not None else "已返回"
                summaries.append(f"调 {function['name']}({str(target)[:80]}) → {outcome}")
    if calls:
        return []
    if direct_reply:
        # 旧直答有时已把查阅摘要写进正文，不能只省略新拼接的摘要。
        body = re.split(r"(?m)^[ \t]*(?:依据|查阅过程)[：:]", last["content"], maxsplit=1)[0]
        answer = re.sub(r"\[\d+\]", "", body).strip()
        if not answer:
            return []
        return [{"role": "user", "content": turn[0]["content"]},
                {"role": "assistant", "content": answer}]
    citations = _field(last, "citations", None)
    if citations is None:
        citations = _field(last, "citations_json", [])
    labels = {str(item["n"]): item["label"] for item in citations
              if isinstance(item, dict) and "n" in item and "label" in item}
    # 编号是"当轮局部"的，新一轮会重新编号：历史里的 [n] 替换成出处文字，防引用错位
    answer = re.sub(r"\[(\d+)\]", lambda match:
                    f"（见 {labels[match[1]]}）" if match[1] in labels else "（历史引用）",
                    last["content"])
    if summaries:
        answer += "\n查阅过程：" + "、".join(summaries)
    return [{"role": "user", "content": turn[0]["content"]},
            {"role": "assistant", "content": answer}]


def assemble(history: list[dict], question: str, *, direct_reply: bool = False) -> list[dict]:
    """全量落库历史 + 本问题 → LLM messages。"""
    turns: list[list[dict]] = []
    for message in history:
        if message.get("role") == "user":
            turns.append([])
        if turns:
            turns[-1].append(message)
    compact = [turn for raw in turns
               if (turn := _compact_turn(raw, direct_reply=direct_reply))][-settings.agent_history_turns:]
    while compact and sum(len(message["content"]) for turn in compact
                          for message in turn) > settings.agent_history_budget:
        compact.pop(0)
    system_prompt = prompts.direct_reply_prompt() if direct_reply else prompts.system_prompt()
    return [{"role": "system", "content": system_prompt},
            *(message for turn in compact for message in turn),
            {"role": "user", "content": question}]


def append_round(rounds: list[list[dict]], assistant: dict, results: list[dict]) -> None:
    """把一轮完整的 调用+结果 追加进本轮工具区；超预算时裁块/淘汰最旧轮。"""
    payloads = [dict(result, blocks=list(result.get("blocks", []))) for result in results]

    def messages() -> list[dict]:
        return [assistant, *[
            {"role": "tool", "tool_call_id": call["id"], "content": encode(payload)}
            for call, payload in zip(assistant["tool_calls"], payloads)
        ]]

    exchange = messages()
    while len(encode([exchange])) > settings.agent_tools_budget:
        largest = max(payloads, key=lambda payload: len(encode(payload.get("blocks", []))))
        if not largest["blocks"]:
            return  # 参数本身就超预算：整轮放弃
        largest["blocks"].pop()
        largest["omitted"] = True
        exchange = messages()

    rounds.append(exchange)
    while len(encode(rounds)) > settings.agent_tools_budget:
        rounds.pop(0)


def final_evidence(rounds: list[list[dict]]) -> list[dict]:
    """本轮工具区里仍存活的资料块，按编号排序（供终局组装与引用导出）。"""
    blocks: dict = {}
    for exchange in rounds:
        for message in exchange[1:]:
            for block in json.loads(message["content"]).get("blocks", []):
                n = block["n"]
                if n not in blocks or len(encode(block)) > len(encode(blocks[n])):
                    blocks[n] = block
    return [blocks[n] for n in sorted(blocks)]
