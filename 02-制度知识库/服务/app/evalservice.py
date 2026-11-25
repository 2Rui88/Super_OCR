"""评测服务（链路 E）—— 逐题走真实 Agent 链路，LLM 判分 + 出处比对 + 归因。

与线上问答完全同一条引擎路径（engine + 业务终局组装器），评测即真实链路；
每题同样写入会话历史。五项指标：总正确率 / 出处正确率 / 表格类正确率 /
查无拒答正确率 / 平均耗时。归因规则：
- 检索错：金标文件的块没被任何工具查阅到；
- OCR 错：金标块命中但 source_mode=ocr 且置信度低（<0.85）；
- 回答错：金标块命中且非低置信 OCR，仍答错。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from contextlib import aclosing

from app import llm
from app.agent import engine, store
from app.agent_business import prompt_business
from app.agent_business import setup as business_setup
from app.agent_business.citations import Citations
from app.agent_business.monetary_answer import calculation_stream
from app.config import settings
from app.db import connect

log = logging.getLogger("kb.eval")

_runs: dict[int, asyncio.Task] = {}

_JUDGE_PROMPT = (
    "你是评测判卷员。判断系统回答是否正确，判据是【核心要点】："
    "回答覆盖标准答案的核心要点且无事实错误即算对；比标准答案多出的合理补充不算错；"
    "仅遗漏标准答案的次要细节不算错。数字、金额、条件、对象任一与标准答案矛盾才算错；"
    "且回答'没有找到相关规定'而标准答案有内容时判错。"
    "只输出 JSON：{\"ok\": true, \"reason\": \"依据\"} 或 {\"ok\": false, \"reason\": \"差异点\"}。"
)


def start_run(run_id: int) -> None:
    _runs[run_id] = asyncio.create_task(_execute(run_id))


def run_active(run_id: int) -> bool:
    task = _runs.get(run_id)
    return task is not None and not task.done()


async def _execute(run_id: int) -> None:
    with connect() as db:
        items = db.execute("SELECT * FROM eval_items ORDER BY id").fetchall()
        config = {"reject_threshold": settings.reject_threshold,
                  "chat_model": settings.chat_model,
                  "embed_model": settings.embed_model}
        db.execute("UPDATE eval_runs SET config_json=? WHERE id=?",
                   (json.dumps(config, ensure_ascii=False), run_id))
    for item in items:
        started = time.perf_counter()
        try:
            detail = await _eval_one(item)
        except Exception as exc:
            log.exception("评测题失败 item=%s", item["id"])
            detail = {"ok": None, "citation_ok": 0, "refused": 0,
                      "attribution": "", "answer": "", "error": str(exc)}
        duration = int((time.perf_counter() - started) * 1000)
        with connect() as db:
            db.execute(
                "INSERT INTO eval_results (run_id, item_id, answer, ok, citation_ok,"
                " refused, attribution, detail_json, duration_ms)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, item["id"], detail.get("answer", ""), detail.get("ok"),
                 detail.get("citation_ok", 0), detail.get("refused", 0),
                 detail.get("attribution", ""),
                 json.dumps(detail, ensure_ascii=False), duration))
    summary = summarize(run_id)
    with connect() as db:
        db.execute("UPDATE eval_runs SET finished_at=?, summary_json=? WHERE id=?",
                   (int(time.time()), json.dumps(summary, ensure_ascii=False), run_id))


def _chunk_meta(chunk_ids: set[int]) -> dict[int, dict]:
    """chunk_id → {filename, source_mode, ocr_confidence}（归因用）。"""
    if not chunk_ids:
        return {}
    marks = ",".join("?" * len(chunk_ids))
    with connect() as db:
        rows = db.execute(
            f"SELECT c.id, c.source_mode, c.ocr_confidence, d.filename"
            f" FROM chunks c JOIN documents d ON d.id=c.doc_id WHERE c.id IN ({marks})",
            tuple(chunk_ids)).fetchall()
    return {r["id"]: dict(r) for r in rows}


async def _run_agent(question: str) -> dict:
    """单题跑真实 Agent 链路（与 /qa/ask 同引擎同终局组装器），写会话历史。"""
    business_setup()
    conv_id, user_id = store.begin_turn(None, question)   # 评测同样写入历史
    citations = Citations(question)
    finalize = prompt_business.make_finalize(citations)
    parts: list[str] = []
    citation_items: list[dict] = []
    seen_blocks: dict[int, dict] = {}
    refused = 0
    tool_calls = 0
    error_code: str | None = None
    pending_args: dict[str, dict] = {}
    async with aclosing(engine.run(question, [], stream=calculation_stream(question, citations), tool_ctx=citations,
                                    finalize=finalize)) as events:
        async for ev in events:
            kind = ev["type"]
            if kind == "delta":
                parts.append(ev["text"])
            elif kind == "refuse":
                refused = 1
                parts = [ev["message"]]
            elif kind == "citations":
                citation_items = ev["items"]
            elif kind == "tool_call":
                tool_calls += 1
                pending_args[ev["id"]] = ev["args"]
            elif kind == "tool_result":
                for block in ev["result"].get("blocks", []):
                    if block.get("chunk_id") is not None:
                        seen_blocks[block["chunk_id"]] = block
                await asyncio.to_thread(
                    store.save_tool_round, conv_id, ev["id"], ev["name"],
                    pending_args.pop(ev["id"], {}), ev["result"])
            elif kind == "error":
                error_code = ev["code"]
    answer = "".join(parts)
    await asyncio.to_thread(store.save_answer, conv_id, user_id, answer,
                            citation_items, error_code)
    return {"answer": answer, "refused": refused, "citations": citation_items,
            "seen": seen_blocks, "tool_calls": tool_calls,
            "error": error_code, "conversation_id": conv_id}


async def _eval_one(item) -> dict:
    question = item["question"]
    gold_files = json.loads(item["gold_files"] or "[]")
    expect_refuse = item["category"] == "查无"

    agent = await _run_agent(question)
    answer, refused = agent["answer"], agent["refused"]
    meta = _chunk_meta(set(agent["seen"]))
    retrieved_files = sorted({m["filename"] for m in meta.values()})
    if agent["error"]:
        return {"ok": None, "citation_ok": 0, "refused": refused, "attribution": "",
                "answer": answer, "error": agent["error"],
                "retrieved_files": retrieved_files[:5],
                "conversation_id": agent["conversation_id"]}

    refused_final = bool(refused) or ("没有找到" in answer)
    if expect_refuse:
        # 查无题：拒答即成功（引擎试尽后拒 / 生成层按铁律拒，都算）
        return {"ok": 1 if refused_final else 0,
                "refused": 1 if refused_final else 0, "citation_ok": None,
                "attribution": "" if refused_final else "generation",
                "answer": answer, "retrieved_files": retrieved_files[:5],
                "tool_calls": agent["tool_calls"],
                "conversation_id": agent["conversation_id"]}
    if refused_final:  # 不该拒答却拒了
        gold_retrieved = (not gold_files) or bool(set(gold_files) & set(retrieved_files))
        return {"ok": 0, "refused": 1, "citation_ok": 0,
                "attribution": "retrieval" if not gold_retrieved else "generation",
                "answer": answer, "retrieved_files": retrieved_files[:5],
                "tool_calls": agent["tool_calls"],
                "conversation_id": agent["conversation_id"]}

    # 出处比对：回答中的 [n] → 引用注册表 chunk → 文件 ∈ 金标
    valid_ns = {c["n"] for c in agent["citations"]}
    cited_ns = sorted({int(n) for n in re.findall(r"\[(\d+)\]", answer)
                       if int(n) in valid_ns})
    n_to_chunk = {c["n"]: c["chunk_id"] for c in agent["citations"]}
    cited_files = sorted({meta[n_to_chunk[n]]["filename"] for n in cited_ns
                          if n in n_to_chunk and n_to_chunk[n] in meta})
    if gold_files:
        citation_ok = 1 if set(cited_files) & set(gold_files) else 0
    else:
        citation_ok = 1 if cited_ns else 0
    gold_retrieved = (not gold_files) or bool(set(gold_files) & set(retrieved_files))

    # LLM 判答案对错
    verdict_raw = await llm.chat([
        {"role": "system", "content": _JUDGE_PROMPT},
        {"role": "user", "content":
            f"问题：{question}\n标准答案：{item['gold_answer']}\n系统回答：{answer}"}])
    ok = None
    m = re.search(r"\{[^{}]*\"ok\"[^{}]*\}", verdict_raw, re.S)
    if m:
        try:
            ok = 1 if json.loads(m.group(0))["ok"] else 0
        except Exception:
            ok = None

    # 归因
    attribution = ""
    if ok == 0:
        if not gold_retrieved:
            attribution = "retrieval"
        elif any(m2["source_mode"] == "ocr" and (m2["ocr_confidence"] or 1.0) < 0.85
                 for m2 in meta.values() if m2["filename"] in gold_files):
            attribution = "ocr"
        else:
            attribution = "generation"

    return {"ok": ok, "citation_ok": citation_ok, "refused": 0,
            "attribution": attribution, "answer": answer,
            "cited": cited_ns, "cited_files": cited_files,
            "retrieved_files": retrieved_files[:5], "judge": verdict_raw[:200],
            "tool_calls": agent["tool_calls"],
            "conversation_id": agent["conversation_id"]}


def summarize(run_id: int) -> dict:
    """五项指标 + 分题型统计。"""
    with connect() as db:
        rows = db.execute(
            "SELECT e.category, r.ok, r.citation_ok, r.refused, r.attribution,"
            " r.duration_ms FROM eval_results r"
            " JOIN eval_items e ON e.id = r.item_id WHERE r.run_id=?",
            (run_id,)).fetchall()
    total = len(rows)
    answered = [r for r in rows if r["ok"] is not None]
    cite_rows = [r for r in rows if r["citation_ok"] is not None]
    refuse_rows = [r for r in rows if r["category"] == "查无"]
    table_rows = [r for r in rows if r["category"] == "表格"]
    by_cat: dict[str, dict] = {}
    for r in rows:
        c = by_cat.setdefault(r["category"], {"total": 0, "ok": 0})
        c["total"] += 1
        c["ok"] += 1 if r["ok"] == 1 else 0
    return {
        "total": total,
        "accuracy": _rate(sum(1 for r in answered if r["ok"] == 1), len(answered)),
        "citation_rate": _rate(sum(1 for r in cite_rows if r["citation_ok"] == 1),
                               len(cite_rows)),
        "table_accuracy": _rate(sum(1 for r in table_rows if r["ok"] == 1),
                                len(table_rows)),
        "refuse_accuracy": _rate(sum(1 for r in refuse_rows if r["ok"] == 1),
                                 len(refuse_rows)),
        "mean_duration_ms": int(sum(r["duration_ms"] or 0 for r in rows) / total)
                            if total else 0,
        "by_category": by_cat,
        "attribution": {
            kind: sum(1 for r in rows if r["attribution"] == kind)
            for kind in ("ocr", "retrieval", "generation")},
    }


def _rate(a: int, b: int) -> float | None:
    return round(a / b, 4) if b else None
