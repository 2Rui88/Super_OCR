"""ReAct 循环引擎（内核）—— 查阅循环 + 终局生成，零业务依赖。

run() 是纯异步生成器：只产出事件，不落库、不碰 HTTP；路由层翻译成 SSE，
评测层直接消费。业务经三个注入口进入：
  registry.register()   注册工具（能力清单随之同步）
  prompts.register_*()  注入提示词段 / 拒答话术
  run(tool_ctx=, finalize=)  工具上下文对象 + 终局组装器
全部缺省时引擎退化为纯流式对话（内核自验证形态）。
"""
from __future__ import annotations

import asyncio
import json
import re
import logging
import sys
from contextlib import aclosing
from collections.abc import Callable

from app import llm
from app.agent import context, events, prompts, registry
from app.config import settings

log = logging.getLogger("kb.agent")

# asyncio.timeout_at 是 3.11+ API；服务器为 3.10，用等价实现兜底（接口含 expired()）
if sys.version_info >= (3, 11):
    _timeout_at = asyncio.timeout_at
else:
    class _TimeoutAt:
        """到点取消当前任务并转成 TimeoutError；expired() 供调用方判别超时来源。"""

        def __init__(self, deadline: float):
            self._deadline = deadline
            self._handle = None
            self._task = None
            self._fired = False

        def expired(self) -> bool:
            return self._fired

        def _fire(self) -> None:
            self._fired = True
            if self._task is not None:
                self._task.cancel()

        async def __aenter__(self):
            loop = asyncio.get_running_loop()
            if loop.time() >= self._deadline:
                self._fired = True
                raise TimeoutError
            self._task = asyncio.current_task()
            self._handle = loop.call_at(self._deadline, self._fire)
            return self

        async def __aexit__(self, exc_type, exc, tb):
            if self._handle is not None:
                self._handle.cancel()
            if exc_type is asyncio.CancelledError and self._fired:
                raise TimeoutError from None
            return False

    _timeout_at = _TimeoutAt


class _IncompleteResponse(ValueError):
    pass


async def _plan(stream, messages: list[dict]) -> tuple[list[tuple[dict, dict]], str]:
    """流式收集模型本轮的工具调用与本轮文本。

    返回 (工具调用列表, 本轮文本)。零工具调用时的文本供直答路径使用
    （闲聊：模型不调工具直接输出回答）；与工具调用同轮的文本维持原行为（前言）。
    分片宽容拼装（qwen 流式实测行为）：index 允许跳号、id 允许只在首片出现、
    arguments 允许空串；完整性以"名称非空 + 参数可解析"为准。
    """
    parts: dict[int, dict] = {}
    expects_calls = False
    text_parts: list[str] = []
    async with aclosing(stream(messages, tools=registry.tool_definitions())) as response:
        async for fragment in response:
            if isinstance(fragment, str):
                text_parts.append(fragment)
                continue
            if fragment.get("finish_reason") == "tool_calls":
                expects_calls = True
                continue
            if fragment.get("finish_reason") == "length" and not parts:
                break  # 查阅规划阶段的长文本草稿触顶：视作本轮无调用
            index = fragment.get("index")
            if type(index) is not int or index < 0:
                raise _IncompleteResponse("查阅阶段的模型响应不完整。")
            if fragment.get("type") not in (None, "function"):
                raise _IncompleteResponse("模型返回了无法识别的工具调用。")
            part = parts.setdefault(index, {"id": "", "name": "", "arguments": ""})
            function = fragment.get("function") or {}
            part["id"] += fragment.get("id") or ""
            part["name"] += function.get("name") or ""
            part["arguments"] += function.get("arguments") or ""
    if expects_calls and not parts:
        raise _IncompleteResponse("模型返回的工具调用不完整。")
    calls: list[tuple[dict, dict]] = []
    for seq, (_, part) in enumerate(sorted(parts.items())):
        raw = part["arguments"].strip() or "{}"
        # 模型模仿历史参数时偶尔输出 Python 字面量（None/True/False），先归一成 JSON
        raw = re.sub(r"\b(None|True|False)\b",
                     lambda m: {"None": "null", "True": "true", "False": "false"}[m.group(1)],
                     raw)
        try:
            args = json.loads(raw)
        except ValueError as exc:
            log.warning("工具参数解析失败 parts=%s",
                        context.encode({k: {**v, "arguments": v["arguments"][:200]}
                                        for k, v in parts.items()})[:800])
            raise _IncompleteResponse("模型返回的工具参数不完整。") from exc
        name = part["name"]
        if not name or not isinstance(args, dict):
            raise _IncompleteResponse("模型返回的工具调用不完整。")
        call_id = part["id"] or f"call-{name}-{seq}"
        spec = registry.TOOLS.get(name)
        sanitized = spec.arguments(args) if spec else {}
        calls.append(({"id": call_id, "type": "function",
                       "function": {"name": name,
                                    "arguments": context.encode(sanitized)}},
                      sanitized))
    return calls, "".join(text_parts)


async def _answer(stream, messages: list[dict], deadline: float):
    """终局流式生成；命中拒答话术时整体转成 refuse 事件（前缀检测）。"""
    refusal = prompts.refusal()
    prefix: str | None = ""      # 只暂存"可能是拒答开头"的前缀
    clock = asyncio.get_running_loop().time
    async with aclosing(stream(messages, tools=[])) as response:
        while True:
            if clock() >= deadline:
                raise TimeoutError
            try:
                async with _timeout_at(deadline):
                    fragment = await anext(response)
            except StopAsyncIteration:
                break
            if not isinstance(fragment, str):
                raise _IncompleteResponse("模型未完整输出终答。")
            if prefix is None:
                yield {"type": events.DELTA, "text": fragment}
                continue
            prefix += fragment
            candidate = prefix.strip().rstrip("。.!！")
            if refusal and candidate.startswith(refusal):
                # 以拒答话术开头（可能拖带解释/引用）→ 整条按拒答处理，
                # 只保留那句话，后续内容丢弃——拒答不挂"依据"
                yield {"type": events.REFUSE, "message": refusal}
                return
            if refusal and len(prefix) <= 64 and refusal.startswith(candidate):
                continue
            yield {"type": events.DELTA, "text": prefix}
            prefix = None
    if prefix is not None:
        candidate = prefix.strip().rstrip("。.!！")
        if refusal and candidate.startswith(refusal):
            yield {"type": events.REFUSE, "message": refusal}
        elif prefix.strip():
            yield {"type": events.DELTA, "text": prefix}
        else:
            raise _IncompleteResponse("模型没有输出终答。")


async def run(question: str, history: list[dict] | None = None, *, stream=None,
              tool_ctx=None,
              finalize: Callable[[list[dict]], tuple[list[dict], list[dict]]] | None = None):
    """执行一轮问答，产出事件流。

    tool_ctx：业务对象，原样传给每个工具 handler（如引用注册表）。
    finalize：终局组装器 (资料块列表) -> (终局messages, 引用items)；
              缺省时直接基于会话上下文作答（无 citations 事件）。
    """
    stream = stream or llm.chat_stream
    clock = asyncio.get_running_loop().time
    started = clock()
    deadline = started + settings.agent_total_timeout
    lookup_deadline = min(started + settings.agent_lookup_timeout, deadline)
    rounds: list[list[dict]] = []
    count = 0
    try:
        messages = context.assemble(history or [], question)
        for round_number in range(1, settings.agent_max_rounds + 1):
            if clock() >= lookup_deadline:
                break
            yield {"type": events.THINKING, "round": round_number}
            lookup = _timeout_at(lookup_deadline)
            try:
                async with lookup:
                    calls, plan_text = await _plan(
                        stream, messages + [m for group in rounds for m in group])
            except TimeoutError:
                if not lookup.expired():
                    raise
                break
            if not calls:
                direct = plan_text.strip()
                if not count and direct and not direct.startswith("查阅完成"):
                    # 直答路径（闲聊/问候）：全程零工具调用，模型文本即最终回答，
                    # 跳过终局组装与引用；拒答话术检测同样生效保持出口统一
                    refusal = prompts.refusal()
                    if refusal and direct.rstrip("。.!！") == refusal:
                        yield {"type": events.REFUSE, "message": refusal}
                    else:
                        yield {"type": events.DELTA, "text": direct}
                    yield {"type": events.DONE,
                           "duration_ms": int((clock() - started) * 1000),
                           "tool_calls": 0}
                    return
                break  # 已调过工具→「查阅完成」；或空响应→ 终局（空资料拒答兜底）
            results = []
            assistant = {"role": "assistant", "content": None,
                         "tool_calls": [call for call, _ in calls]}
            for call, args in calls:
                if clock() >= lookup_deadline:
                    break
                name = call["function"]["name"]
                spec = registry.TOOLS.get(name)
                count += 1
                yield {"type": events.TOOL_CALL, "id": call["id"], "name": name, "args": args}
                lookup = _timeout_at(lookup_deadline)
                try:
                    async with lookup:
                        result = (await spec.handler(args, tool_ctx) if spec else
                                  {"message": "未知工具，请从能力清单选择。"})
                except TimeoutError:
                    if not lookup.expired():
                        raise
                    break
                results.append(result)
                yield {"type": events.TOOL_RESULT, "id": call["id"], "name": name,
                       "count": result.get("count", 0),
                       "summary": result.get("message", ""), "result": result}
            if results:
                completed = {**assistant, "tool_calls": assistant["tool_calls"][:len(results)]}
                context.append_round(rounds, completed, results)
            if len(results) < len(calls):
                break  # 超时截断的半轮：不再进入下一轮查阅

        if clock() >= deadline:
            raise TimeoutError
        evidence = context.final_evidence(rounds)
        if finalize is None:
            final_messages, citation_items = (
                messages + [m for group in rounds for m in group], None)
        else:
            final_messages, citation_items = finalize(evidence, messages)
        if citation_items is not None:
            yield {"type": events.CITATIONS, "items": citation_items, "tool_calls": count}
        async with aclosing(_answer(stream, final_messages, deadline)) as answer:
            async for event in answer:
                yield event
        yield {"type": events.DONE, "duration_ms": int((clock() - started) * 1000),
               "tool_calls": count}
    except TimeoutError:
        yield {"type": events.ERROR, "code": "timeout", "message": "本次问答超时，回答未完成。"}
    except _IncompleteResponse as exc:
        yield {"type": events.ERROR, "code": "incomplete_model_response", "message": str(exc)}
    except Exception:
        log.exception("Agent 问答失败")
        yield {"type": events.ERROR, "code": "agent_error", "message": "本次问答未完成，请重试。"}
