"""问答 SSE 端点（薄壳）—— 翻译引擎事件、按步落库、断线补存。"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import aclosing
from pathlib import Path
from weakref import WeakValueDictionary

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from app import retrieval
from app.agent import engine, store
from app.agent_business import prompt_business, setup as business_setup
from app.agent_business.citations import Citations
from app.config import settings
from app.db import connect

router = APIRouter(tags=["qa"])
log = logging.getLogger("kb.qa")
_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()
_pending_saves: dict[int, asyncio.Task] = {}


def _conversation_lock(conversation_id: int) -> asyncio.Lock:
    lock = _locks.get(conversation_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[conversation_id] = lock
    return lock


class AskRequest(BaseModel):
    question: str
    conversation_id: int | None = None


def _hits_text(result: dict | None) -> str:
    """轨迹命中位置摘要："文件名·第N页" 去重(最多3个)——查阅过程展示用。"""
    import re as _re
    seen, parts = set(), []
    for block in (result or {}).get("blocks", []):
        label = str(block.get("label") or "")
        if not label:
            continue
        file_part = label.split("·")[0].strip()
        m = _re.search(r"第(\d+)页", label)
        key = file_part + (f"·第{m.group(1)}页" if m else "")
        if key and key not in seen:
            seen.add(key)
            parts.append(key)
    return "、".join(parts[:3]) + ("…" if len(parts) > 3 else "")


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _save_finished(conversation_id: int, task: asyncio.Task) -> None:
    if _pending_saves.get(conversation_id) is task:
        _pending_saves.pop(conversation_id)
    if not task.cancelled() and (error := task.exception()):
        log.error("断线答复保存失败，conversation=%s", conversation_id, exc_info=error)


@router.post("/qa/ask")
async def ask(req: AskRequest):
    """Agent 事件 → SSE；工具对随发生成随落库，终答幂等落库。"""
    business_setup()
    question = req.question.strip()
    if not question:
        raise HTTPException(422, "请输入问题")

    async def gen():
        conv_id = req.conversation_id
        user_id = None
        if conv_id is not None and not store.conversation_exists(conv_id):
            yield _sse({"type": "error", "message": "会话不存在", "conversation_id": conv_id})
            return
        if conv_id is None:
            conv_id, user_id = store.begin_turn(None, question)
        async with _conversation_lock(conv_id):
            parts: list[str] = []
            citations: list[dict] = []
            pending_args: dict[str, dict] = {}
            settled = False
            incomplete_status = "disconnected"
            try:
                if previous_save := _pending_saves.get(conv_id):
                    await asyncio.shield(previous_save)
                if user_id is None:
                    _, user_id = store.begin_turn(conv_id, question)
                history = await asyncio.to_thread(
                    store.load_history, conv_id,
                    before_id=user_id, include_tools=True)
                registry = Citations()
                finalize = prompt_business.make_finalize(registry)
                async with aclosing(
                        engine.run(question, history,
                                   tool_ctx=registry, finalize=finalize)) as events:
                    async for event in events:
                        kind = event["type"]
                        if kind == "tool_call":
                            pending_args[event["id"]] = event["args"]
                        elif kind == "tool_result":
                            await asyncio.to_thread(
                                store.save_tool_round, conv_id, event["id"], event["name"],
                                pending_args.pop(event["id"]), event["result"])
                            event["hits"] = _hits_text(event.get("result"))
                        elif kind == "citations":
                            citations = event["items"]
                        elif kind == "delta":
                            parts.append(event["text"])
                        elif kind == "refuse":
                            parts[:] = [event["message"]]
                        elif kind in {"done", "error"}:
                            status = None if kind == "done" else event["code"]
                            await asyncio.to_thread(
                                store.save_answer, conv_id, user_id, "".join(parts),
                                citations, status)
                            settled = True
                        public = {key: value for key, value in event.items()
                                  if key != "result"}
                        yield _sse({**public, "conversation_id": conv_id})
            except Exception:
                incomplete_status = "agent_error"
                log.exception("问答会话处理失败，conversation=%s", conv_id)
                yield _sse({"type": "error", "code": "agent_error",
                            "message": "本次问答未完成，请重试。", "conversation_id": conv_id})
            finally:
                if user_id is not None and not settled:
                    task = asyncio.create_task(asyncio.to_thread(
                        store.save_answer, conv_id, user_id, "".join(parts),
                        citations, incomplete_status))
                    _pending_saves[conv_id] = task
                    task.add_done_callback(lambda done: _save_finished(conv_id, done))
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        pass

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- 出处（链路 C）

@router.get("/chunks/{chunk_id}")
def chunk_detail(chunk_id: int):
    """出处点击：一个 chunk_id 锁死一切——分流渲染所需的全量信息。"""
    with connect() as db:
        row = db.execute(
            "SELECT c.id, c.doc_id, c.page_num, c.bbox, c.type, c.row_index, c.heading_path,"
            " c.seek_text, c.html, c.crop_path, c.linear_text, c.vl_desc, c.source_mode,"
            " c.ocr_confidence, d.filename"
            " FROM chunks c JOIN documents d ON d.id = c.doc_id WHERE c.id=?",
            (chunk_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "块不存在")
    d = dict(row)
    d["bbox"] = json.loads(d["bbox"])
    d["label"] = retrieval.citation_label(d)
    return d


@router.get("/documents/{doc_id}/page/{page_num}.png")
def page_image(doc_id: int, page_num: int):
    """出处抽屉页图：渲染 PDF 指定页(144dpi,磁盘缓存),供 bbox 高亮定位。"""
    with connect() as db:
        row = db.execute("SELECT pdf_path FROM documents WHERE id=?",
                         (doc_id,)).fetchone()
    if row is None or not row["pdf_path"]:
        raise HTTPException(404, "文档不存在")
    pdf_path = Path(row["pdf_path"])
    if not pdf_path.exists():
        raise HTTPException(404, "原文件已不存在")
    cache_dir = settings.data_dir / "pages"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache = cache_dir / f"{doc_id}_{page_num}.png"
    if not cache.exists():
        import pymupdf
        try:
            with pymupdf.open(pdf_path) as doc:
                if not 1 <= page_num <= doc.page_count:
                    raise HTTPException(404, f"页码超范围(共 {doc.page_count} 页)")
                doc[page_num - 1].get_pixmap(dpi=144, alpha=False).save(cache)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(500, f"页面渲染失败:{exc}")
    return FileResponse(cache, media_type="image/png")


@router.get("/documents/{doc_id}/file")
def download_original(doc_id: int):
    """下载原件（优先原始上传件，其次转换 PDF）。"""
    with connect() as db:
        row = db.execute("SELECT filename, pdf_path FROM documents WHERE id=?",
                         (doc_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "文档不存在")
    path = Path(row["pdf_path"])
    if not path.exists():
        raise HTTPException(404, "文件已不存在")
    return FileResponse(path, filename=row["filename"])


@router.get("/crops/{chunk_id}")
def crop_image(chunk_id: int):
    """流程图裁图（出处抽屉展示用）。"""
    with connect() as db:
        row = db.execute("SELECT crop_path FROM chunks WHERE id=?", (chunk_id,)).fetchone()
    if row is None or not row["crop_path"] or not Path(row["crop_path"]).exists():
        raise HTTPException(404, "裁图不存在")
    return FileResponse(row["crop_path"], media_type="image/png")


# ---------------------------------------------------------------- 会话

@router.get("/conversations")
def list_conversations():
    with connect() as db:
        rows = db.execute(
            "SELECT c.id, c.title, c.created_at,"
            " (SELECT COUNT(*) FROM messages m WHERE m.conversation_id=c.id) msg_count"
            " FROM conversations c ORDER BY c.id DESC LIMIT 50").fetchall()
        return {"conversations": [dict(r) for r in rows]}


@router.get("/conversations/{conv_id}/messages")
def conversation_messages(conv_id: int, include_tools: bool = False):
    messages = store.load_history(conv_id, include_tools=include_tools)
    if not messages:
        raise HTTPException(404, "会话不存在或无消息")
    return {"messages": messages}


@router.delete("/conversations/{conv_id}")
async def delete_conversation(conv_id: int):
    async with _conversation_lock(conv_id):
        if previous_save := _pending_saves.get(conv_id):
            await asyncio.shield(previous_save)
        ok = await asyncio.to_thread(store.delete_conversation, conv_id)
        if not ok:
            raise HTTPException(404, "会话不存在")
        return {"ok": True}
