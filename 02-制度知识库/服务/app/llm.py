"""DashScope 客户端 —— OpenAI 兼容端点，httpx 直连（不引 SDK）。

供问答、Agent 工具调用、评测和流程图描述使用。
全局限流：Semaphore(8) + 429 指数退避；密钥仅从环境变量 DASHSCOPE_API_KEY 读取，
不落库、不下发、不出现在日志。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import os
from pathlib import Path

import httpx

from app.config import settings

log = logging.getLogger("kb.llm")

_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
# 原生端点（模型名不在兼容端点开放）
_EMB_URL = ("https://dashscope.aliyuncs.com/api/v1/services/"
            "embeddings/multimodal-embedding/multimodal-embedding")
_VL_URL = ("https://dashscope.aliyuncs.com/api/v1/services/"
           "aigc/multimodal-generation/generation")
_MAX_RETRIES = 3
_gate: asyncio.Semaphore | None = None


class _Retry429(Exception):
    pass


def _normalize(vec: list[float]) -> list[float]:
    """L2 归一（余弦检索靠 FlatIP + 归一化向量实现）。"""
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _key() -> str:
    return os.environ.get("DASHSCOPE_API_KEY", "")


def available() -> bool:
    return bool(_key())


def _sem() -> asyncio.Semaphore:
    global _gate
    if _gate is None:
        _gate = asyncio.Semaphore(8)  # 全局并发闸门
    return _gate


class LLMUnavailable(RuntimeError):
    """未配置 DASHSCOPE_API_KEY —— 调用方必须走降级路径，不允许抛崩摄入。"""


async def _request(client: httpx.AsyncClient, method: str, url: str,
                   payload: dict) -> dict:
    for attempt in range(_MAX_RETRIES + 1):
        async with _sem():
            resp = await client.request(method, url, json=payload,
                                        headers={"Authorization": f"Bearer {_key()}"})
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429 and attempt < _MAX_RETRIES:
            wait = min(2 ** attempt, 8)
            log.warning("429 限流，%ds 后重试", wait)
            await asyncio.sleep(wait)
            continue
        resp.raise_for_status()
    raise RuntimeError("unreachable")


async def embed(texts: list[str]) -> list[list[float]]:
    """批量向量化：原生多模态 embedding 端点，单文本单请求，
    Semaphore 并发 + 429/限流退避。返回与输入等长的向量列表（已 L2 归一）。"""
    if not available():
        raise LLMUnavailable("未配置 DASHSCOPE_API_KEY")
    if not texts:
        return []

    async def _one(client: httpx.AsyncClient, text: str) -> list[float]:
        body = {"model": settings.embed_model,
                "input": {"contents": [{"text": text}]},
                "parameters": {"dimension": settings.embed_dim}}
        data = await _native_request(client, _EMB_URL, body)
        return data["output"]["embeddings"][0]["embedding"]

    async with httpx.AsyncClient(timeout=60) as client:
        vectors = await asyncio.gather(*[_one(client, t) for t in texts])
    return [_normalize(v) for v in vectors]


async def chat(messages: list[dict], *, temperature: float = 0.3,
               max_tokens: int = 2048) -> str:
    """非流式生成（改写/判分等受控点位）。

    qwen3.5 系为思考模型：显式关思考；偶发只回 reasoning 时兜底取 reasoning_content。
    """
    if not available():
        raise LLMUnavailable("未配置 DASHSCOPE_API_KEY")
    async with httpx.AsyncClient(timeout=120) as client:
        res = await _request(client, "POST", f"{_BASE}/chat/completions", {
            "model": settings.chat_model, "messages": messages,
            "temperature": temperature, "max_tokens": max_tokens,
            "enable_thinking": False,
        })
    msg = res["choices"][0]["message"]
    return msg.get("content") or msg.get("reasoning_content") or ""


async def chat_stream(messages: list[dict], *, temperature: float = 0.3,
                      max_tokens: int = 2048, tools: list[dict] | None = None):
    """流式返回文本。启用工具时也返回含 index 的工具调用片段。"""
    if not available():
        raise LLMUnavailable("未配置 DASHSCOPE_API_KEY")
    payload = {"model": settings.chat_model, "messages": messages,
               "temperature": temperature, "max_tokens": max_tokens,
               "stream": True, "enable_thinking": False}
    if tools:
        payload.update(tools=tools, tool_choice="auto")
    for attempt in range(_MAX_RETRIES + 1):
        try:
            async with _sem():
                async with httpx.AsyncClient(timeout=180) as client:
                    async with client.stream("POST", f"{_BASE}/chat/completions", json=payload,
                                             headers={"Authorization": f"Bearer {_key()}"}) as resp:
                        if resp.status_code == 429:
                            raise _Retry429()
                        resp.raise_for_status()
                        async for line in resp.aiter_lines():
                            if not line.startswith("data: "):
                                continue
                            data = line[6:]
                            if data.strip() == "[DONE]":
                                return
                            chunk = json.loads(data)
                            if not chunk.get("choices"):  # 末尾 usage 帧无 choices
                                continue
                            choice = chunk["choices"][0]
                            delta = choice.get("delta") or {}
                            if delta.get("content"):
                                yield delta["content"]
                            if tools is not None:
                                for call in delta.get("tool_calls") or []:
                                    yield call
                                if choice.get("finish_reason") in {"tool_calls", "length", "content_filter"}:
                                    yield {"finish_reason": choice["finish_reason"]}
                        return
        except _Retry429:
            if attempt >= _MAX_RETRIES:
                raise
            wait = min(2 ** attempt, 8)
            log.warning("流式 429，%ds 后重试", wait)
            await asyncio.sleep(wait)


async def _native_request(client: httpx.AsyncClient, url: str, payload: dict) -> dict:
    """原生端点请求：429/Throttling 指数退避（原生 API 限流常以 body.code 返回）。"""
    for attempt in range(_MAX_RETRIES + 1):
        async with _sem():
            resp = await client.post(url, json=payload,
                                     headers={"Authorization": f"Bearer {_key()}"})
        if resp.status_code == 200:
            data = resp.json()
            if str(data.get("code", "")).lower().startswith("throttl") and attempt < _MAX_RETRIES:
                await asyncio.sleep(min(2 ** attempt, 8))
                continue
            return data
        if resp.status_code == 429 and attempt < _MAX_RETRIES:
            await asyncio.sleep(min(2 ** attempt, 8))
            continue
        resp.raise_for_status()
    raise RuntimeError("unreachable")


async def vl_describe(image_path: str | Path, prompt: str) -> str:
    """流程图结构描述（A6 唯一 VL 用途）。原生 multimodal-generation，图片走 base64。"""
    if not available():
        raise LLMUnavailable("未配置 DASHSCOPE_API_KEY")
    b64 = base64.b64encode(Path(image_path).read_bytes()).decode()
    body = {
        "model": settings.vl_model,
        "input": {"messages": [{"role": "user", "content": [
            {"image": f"data:image/png;base64,{b64}"},
            {"text": prompt},
        ]}]},
    }
    async with httpx.AsyncClient(timeout=120) as client:
        data = await _native_request(client, _VL_URL, body)
    content = data["output"]["choices"][0]["message"]["content"]
    if isinstance(content, list):  # 多模态返回为分片列表
        return "".join(part.get("text", "") for part in content)
    return str(content)
