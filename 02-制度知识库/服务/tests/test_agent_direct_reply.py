import asyncio
import unittest
from contextlib import aclosing
from unittest.mock import patch

from app.agent import context, engine, prompts, registry
from app.agent_business import prompt_business


class DirectReplyTest(unittest.IsolatedAsyncioTestCase):
    async def test_direct_reply_streams_with_clean_history(self):
        call = {"id": "previous", "function": {
            "name": "search_chunks", "arguments": {"query": "旧检索词"},
        }}
        history = [
            {"role": "user", "content": "住宿费是多少？"},
            {"role": "assistant", "tool_calls": [call]},
            {"role": "tool", "tool_call_id": "previous", "content": '{"count":6}'},
            {"role": "assistant", "content": "住宿费见 [4]。\n\n依据：旧文件第2页",
             "citations": [{"n": 4, "label": "旧文件第2页"}]},
            {"role": "user", "content": "还有呢？"},
            {"role": "assistant", "content": "旧回答。\n查阅过程：调 search_chunks(旧问题) → 6块"},
        ]
        question = "二类城市有哪些？"
        received = []
        continue_reply = asyncio.Event()

        async def model(messages, *, tools):
            received.append((messages, tools))
            if len(received) == 1:
                yield "规划正文，不应该展示。"
                yield "\n查阅过程：调 search_chunks(二类城市) → 6块"
                return
            yield "本轮没有可核实的制度资料，"
            await continue_reply.wait()
            yield "暂时无法确认城市分类。"

        with patch.object(prompts, "_direct_reply", prompt_business.DIRECT_REPLY_RULES):
            async with aclosing(engine.run(question, history, stream=model)) as events:
                self.assertEqual((await anext(events))["type"], "thinking")
                first = await asyncio.wait_for(anext(events), timeout=2)
                self.assertEqual(first, {"type": "delta", "text": "本轮没有可核实的制度资料，"})
                self.assertFalse(continue_reply.is_set())
                continue_reply.set()
                remaining = [event async for event in events]

        self.assertEqual([event["type"] for event in remaining], ["delta", "done"])
        self.assertEqual(remaining[0]["text"], "暂时无法确认城市分类。")
        self.assertEqual(remaining[-1]["tool_calls"], 0)
        self.assertEqual(len(received), 2)
        self.assertEqual(received[0], (context.assemble(history, question), registry.tool_definitions()))
        reply_messages, reply_tools = received[1]
        self.assertEqual(reply_tools, [])
        self.assertEqual(reply_messages[0]["content"], prompt_business.DIRECT_REPLY_RULES)
        self.assertEqual(reply_messages[-1]["content"], question)
        reply_history = '\n'.join(message["content"] for message in reply_messages[1:-1])
        self.assertIn("住宿费是多少？", reply_history)
        for marker in ("查阅过程", "search_chunks", "旧文件", "[4]"):
            self.assertNotIn(marker, reply_history)

    async def test_tool_path_still_retrieves_and_emits_citations(self):
        async def retrieve(args, tool_context):
            self.assertEqual(args, {"query": "差旅"})
            return {"count": 1, "blocks": [{"n": 1, "chunk_id": 11}]}

        tool = registry.ToolSpec(
            "search_chunks", "检索", {"query": {"type": "string"}}, retrieve, "检索资料",
        )
        fragments = iter([
            [{"index": 0, "id": "lookup", "function": {
                "name": "search_chunks", "arguments": '{"query":"差旅"}',
            }}],
            ["查阅完成"],
            ["根据资料 ", "[1]。"],
        ])

        async def model(messages, *, tools):
            for fragment in next(fragments):
                yield fragment

        def finalize(evidence, messages):
            self.assertEqual(evidence, [{"n": 1, "chunk_id": 11}])
            return messages, evidence

        with patch.dict(registry.TOOLS, {tool.name: tool}, clear=True):
            result = [event async for event in engine.run("差旅规定", stream=model, finalize=finalize)]

        self.assertEqual([event["type"] for event in result], [
            "thinking", "tool_call", "tool_result", "thinking", "citations", "delta", "delta", "done",
        ])
        self.assertEqual(result[4]["items"], [{"n": 1, "chunk_id": 11}])
        self.assertEqual(result[-1]["tool_calls"], 1)
