"""真 API 全链路问答测试:消费 SSE,验证事件序/工具调用/引用/拒答/多轮。"""
import asyncio
import json
import sys

import httpx

API = "http://127.0.0.1:8800/api/v1"


async def ask(question: str, conversation_id: int | None = None):
    events = []
    async with httpx.AsyncClient(timeout=180) as client:
        async with client.stream(
                "POST", f"{API}/qa/ask",
                json={"question": question, "conversation_id": conversation_id}) as resp:
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
    return events


def brief(events):
    parts = []
    for ev in events:
        k = ev["type"]
        if k == "tool_call":
            parts.append(f"🔧{ev['name']}({json.dumps(ev['args'], ensure_ascii=False)})")
        elif k == "tool_result":
            parts.append(f"←{ev['count']}块")
        elif k == "citations":
            parts.append(f"📖{len(ev['items'])}条引用")
        elif k == "delta":
            parts.append("…")
        elif k == "refuse":
            parts.append(f"⛔拒答")
        elif k == "done":
            parts.append(f"✅{ev['tool_calls']}次调用/{ev['duration_ms']}ms")
        elif k == "error":
            parts.append(f"❌{ev.get('code')}:{ev.get('message','')[:60]}")
    return " ".join(parts)


def answer_text(events):
    return "".join(ev["text"] for ev in events if ev["type"] == "delta")


async def case(title, question, conversation_id=None, expect_refuse=False):
    events = await ask(question, conversation_id)
    types = [e["type"] for e in events]
    answer = answer_text(events)
    tools = [e["name"] for e in events if e["type"] == "tool_call"]
    cites = next((e["items"] for e in events if e["type"] == "citations"), [])
    ok = "done" in types
    if expect_refuse:
        ok = ok and ("refuse" in types or "没有找到" in answer)
    else:
        ok = ok and "refuse" not in types and len(answer) > 10
    conv = next((e.get("conversation_id") for e in events), None)
    print(f"\n{'='*72}\n[{ 'PASS' if ok else 'FAIL' }] {title}\n  问: {question}")
    print(f"  轨迹: {brief(events)}")
    print(f"  工具: {tools or '（无）'}  引用: {[c['label'][:28] for c in cites][:3]}")
    print(f"  答: {answer[:220]}")
    return ok, conv, cites, events


async def main():
    results = []
    ok, conv, _, _ = await case("①简单事实题(采购合同)", "采购合同里约定的验收标准是什么?")
    results.append(ok)

    ok2, _, _, _ = await case("②表格题(住宿标准)", "员工出差的住宿费标准是怎么规定的?")
    results.append(ok2)

    ok3, _, _, _ = await case("③点名文件题", "在安全生产管理制度中,发生安全事故后应如何处理?")
    results.append(ok3)

    ok4, _, _, _ = await case("④查无题(应拒答)", "公司允许员工带宠物上班吗?有什么规定?",
                              expect_refuse=True)
    results.append(ok4)

    # ⑤ 多轮追问: 同一会话第二问,依赖上文
    ok5a, conv5, _, _ = await case("⑤a 多轮-第一问", "项目验收报告的主要结论是什么?")
    ok5b, _, _, _ = await case("⑤b 多轮-追问(带上文)",
                               "那这份报告里提到的整改要求有哪些?", conversation_id=conv5)
    results.append(ok5a and ok5b)

    print(f"\n{'='*72}\n总结: {sum(results)}/{len(results)} 通过")
    return 0 if all(results) else 1


sys.exit(asyncio.run(main()))
