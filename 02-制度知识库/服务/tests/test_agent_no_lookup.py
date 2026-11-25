import asyncio
import json
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

from app import llm, retrieval
from app.agent import engine, prompts, registry
from app.agent_business import prompt_business, setup
from app.agent_business.citations import Citations
from app.config import settings


QUESTION = "例外转呈流程中，填写处置结果后谁审核？"
CHUNK = {
    "id": 803,
    "filename": "工程设计（咨询）过程控制程序.doc",
    "page_num": 11,
    "heading_path": "4.2.2 例外转程",
    "type": "text",
    "seek_text": "工程设计（咨询）过程控制程序.doc\n填写结果后，由部长核实、总工批准。",
}


class NoLookupTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patches = ExitStack()
        self.addCleanup(patches.close)
        patches.enter_context(patch("app.agent_business._setup_done", False))
        patches.enter_context(patch.dict(registry.TOOLS, clear=True))
        patches.enter_context(patch.object(prompts, "_sections", []))
        patches.enter_context(patch.object(prompts, "_refusal", None))
        patches.enter_context(patch.object(prompts, "_direct_reply", None))
        patches.enter_context(patch.object(engine, "_no_lookup_policy", None))
        self.classify = patches.enter_context(patch.object(llm, "chat", new_callable=AsyncMock))
        self.search = patches.enter_context(patch.object(retrieval, "search", new_callable=AsyncMock))
        self.search.return_value = {"results": [CHUNK]}
        setup()

    async def ask(self, question, plans, *, history=None, answer="由部长核实、总工批准 [1]。"):
        model_calls = []
        plans = iter(plans)

        async def stream(messages, *, tools):
            model_calls.append((messages, tools))
            for fragment in next(plans) if tools else [answer]:
                yield fragment

        citations = Citations()
        events = [event async for event in engine.run(
            question, history, stream=stream, tool_ctx=citations,
            finalize=prompt_business.make_finalize(citations),
        )]
        return events, model_calls

    async def test_zero_lookup_draft_is_replaced_with_search_and_cited_answer(self):
        self.classify.return_value = '{"intent":"lookup","query":null}'
        history = [
            {"role": "user", "content": "例外转呈申请有哪些步骤？"},
            {"role": "assistant", "content": "先填写申请表。"},
        ]
        events, model_calls = await self.ask(QUESTION, [["旧草稿：由设总审核。"]], history=history)

        self.classify.assert_awaited_once()
        classifier_messages = self.classify.await_args.args[0]
        self.assertEqual(json.loads(classifier_messages[-1]["content"]), {
            "question": QUESTION, "history": history,
        })
        self.search.assert_awaited_once_with(QUESTION, doc_id=None, top_k=6)
        self.assertEqual([event["type"] for event in events],
                         ["thinking", "tool_call", "tool_result", "citations", "delta", "done"])
        self.assertEqual(events[1]["id"], events[2]["id"])
        self.assertEqual(events[1]["name"], "search_chunks")
        self.assertEqual(events[3]["items"][0]["chunk_id"], 803)
        self.assertEqual(events[-1]["tool_calls"], 1)
        final_messages, final_tools = model_calls[-1]
        self.assertEqual(final_tools, [])
        self.assertIn("填写结果后，由部长核实、总工批准", final_messages[-1]["content"])
        self.assertTrue(final_messages[-1]["content"].endswith(QUESTION))
        self.assertNotIn("旧草稿", str(final_messages))

    async def test_follow_up_searches_rewritten_query_after_false_lookup_complete(self):
        query = "例外转呈流程中填写处置结果后的下一步"
        self.classify.return_value = json.dumps({"intent": "lookup", "query": query})
        events, model_calls = await self.ask("那下一步呢？", [["查阅完成"]])
        self.search.assert_awaited_once_with(query, doc_id=None, top_k=6)
        self.assertEqual(events[-1]["tool_calls"], 1)
        self.assertTrue(model_calls[-1][0][-1]["content"].endswith("那下一步呢？"))

    async def test_confirmed_chitchat_streams_as_a_direct_answer(self):
        self.classify.return_value = '{"intent":"chitchat","query":null}'
        events, model_calls = await self.ask("你好！", [["规划草稿"]], answer="你好！")
        self.search.assert_not_awaited()
        self.assertEqual([event["type"] for event in events], ["thinking", "delta", "done"])
        self.assertEqual(events[-1]["tool_calls"], 0)
        self.assertEqual(model_calls[-1][0][0]["content"], prompt_business.DIRECT_REPLY_RULES)

    async def test_successful_search_skips_classification_even_when_empty(self):
        self.search.return_value = {"results": []}
        plan = [{"index": 0, "id": "lookup", "function": {
            "name": "search_chunks", "arguments": json.dumps({"query": QUESTION}),
        }}]
        events, _ = await self.ask(QUESTION, [plan, ["查阅完成"]], answer=prompt_business.REFUSAL)
        self.classify.assert_not_awaited()
        self.search.assert_awaited_once()
        self.assertEqual(events[-2]["type"], "refuse")
        self.assertEqual(events[-1]["tool_calls"], 1)

    async def test_uncertain_or_invalid_decision_searches_original_question(self):
        for raw in (
            '{"intent":"uncertain","query":null}', "无法确定",
            '{"intent":"chitchat","query":42}', '{"intent":"chitchat"}',
        ):
            with self.subTest(raw=raw):
                self.search.reset_mock()
                self.classify.return_value = raw
                events, _ = await self.ask(QUESTION, [[prompt_business.REFUSAL]])
                self.search.assert_awaited_once_with(QUESTION, doc_id=None, top_k=6)
                self.assertEqual(events[-1]["tool_calls"], 1)
                self.assertFalse(any(event["type"] == "refuse" for event in events))

    async def test_classifier_timeout_searches_original_question(self):
        async def stall(*args, **kwargs):
            await asyncio.Event().wait()

        self.classify.side_effect = stall
        with patch.object(engine, "_NO_LOOKUP_DECISION_SECONDS", 0.01):
            events, _ = await self.ask(QUESTION, [[]])
        self.search.assert_awaited_once_with(QUESTION, doc_id=None, top_k=6)
        self.assertEqual(events[-1]["tool_calls"], 1)

    async def test_failed_tool_at_round_limit_still_gets_one_real_search(self):
        self.classify.return_value = '{"intent":"lookup","query":null}'
        plan = [{"index": 0, "id": "wrong", "function": {
            "name": "read_figure", "arguments": '{"chunk_id":1}',
        }}]
        with patch.object(settings, "agent_max_rounds", 1), patch(
            "app.agent_business.tools_knowledge._open_chunk", return_value=None,
        ):
            events, _ = await self.ask(QUESTION, [plan])
        self.search.assert_awaited_once_with(QUESTION, doc_id=None, top_k=6)
        self.assertEqual([event["name"] for event in events if event["type"] == "tool_call"],
                         ["read_figure", "search_chunks"])
        self.assertEqual(events[-1]["tool_calls"], 2)

    async def test_failed_search_is_an_error_not_a_refusal(self):
        self.classify.return_value = '{"intent":"lookup","query":null}'
        self.search.side_effect = RuntimeError("search error")
        with self.assertLogs("kb.agent", level="ERROR"):
            events, _ = await self.ask(QUESTION, [["查阅完成"]])
        self.assertEqual(events[-1]["code"], "lookup_failed")
        self.assertFalse(any(event["type"] == "refuse" for event in events))
