"""会话存储（内核）—— messages 落库/读取，HTTP 无关，引擎与路由共用。

写入契约：用户消息先落库；工具调用对（assistant.tool_calls + 全部 tool
回应）同一事务原子写；终答幂等写（以 turn:{user_id} 为键防断线补存重复）。
"""
from __future__ import annotations

import json
import time

from app.db import connect


def _now() -> int:
    return int(time.time())


def begin_turn(conversation_id: int | None, question: str) -> tuple[int, int]:
    """取/建会话并写入用户消息，返回 (conversation_id, user_message_id)。"""
    with connect() as db:
        now = _now()
        if conversation_id is None:
            conversation_id = db.execute(
                "INSERT INTO conversations (title, created_at) VALUES (?, ?)",
                (question[:40], now)).lastrowid
        db.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at)"
            " VALUES (?, 'user', ?, ?)", (conversation_id, question, now))
        user_id = db.execute(
            "SELECT id FROM messages WHERE conversation_id=? AND role='user'"
            " ORDER BY id DESC LIMIT 1", (conversation_id,)).fetchone()["id"]
    return conversation_id, user_id


def conversation_exists(conversation_id: int) -> bool:
    with connect() as db:
        return bool(db.execute(
            "SELECT 1 FROM conversations WHERE id=?", (conversation_id,)).fetchone())


def save_tool_round(conversation_id: int, call_id: str, name: str,
                    args: dict, result: dict) -> None:
    """一对 工具调用+结果 同事务写入（半批永不落库）。"""
    call = {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}
    with connect() as db:
        db.executemany(
            "INSERT INTO messages (conversation_id, role, content, created_at,"
            " tool_calls, tool_call_id) VALUES (?, ?, ?, ?, ?, ?)",
            [(conversation_id, "assistant", "", _now(),
              json.dumps([call], ensure_ascii=False), None),
             (conversation_id, "tool", json.dumps(result, ensure_ascii=False),
              _now(), None, call_id)])


def save_answer(conversation_id: int, user_id: int, answer: str,
                citations: list[dict], status: str | None = None) -> None:
    """终答幂等写入：turn:{user_id} 做唯一键，正常路径与断线补存不重复。"""
    turn_key = f"turn:{user_id}"
    metadata = json.dumps({"status": status}) if status else None
    with connect() as db:
        db.execute(
            "INSERT INTO messages (conversation_id, role, content, citations_json,"
            " created_at, tool_calls, tool_call_id)"
            " SELECT ?, 'assistant', ?, ?, ?, ?, ?"
            " WHERE EXISTS (SELECT 1 FROM messages WHERE id=? AND conversation_id=?)"
            " AND NOT EXISTS (SELECT 1 FROM messages WHERE conversation_id=?"
            " AND role='assistant' AND tool_call_id=?)",
            (conversation_id, answer, json.dumps(citations, ensure_ascii=False),
             _now(), metadata, turn_key, user_id, conversation_id,
             conversation_id, turn_key))


def load_history(conversation_id: int, *, before_id: int | None = None,
                 include_tools: bool = False) -> list[dict]:
    """读取消息（默认给前端/终答视角不含工具行；include_tools=True 返回全量）。

    返回行含 id/role/content/citations(_json)/created_at，工具行附
    tool_calls / tool_call_id；断线补存行带 incomplete 标记。
    """
    query = ("SELECT id, role, content, citations_json, created_at, tool_calls,"
             " tool_call_id FROM messages WHERE conversation_id=?")
    params: tuple = (conversation_id,)
    if before_id is not None:
        query += " AND id<?"
        params += (before_id,)
    with connect() as db:
        rows = db.execute(query + " ORDER BY id", params).fetchall()
    messages = []
    for row in rows:
        calls = json.loads(row["tool_calls"] or "null")
        is_tool_call = row["role"] == "assistant" and isinstance(calls, list)
        if not include_tools and (is_tool_call or row["role"] == "tool"):
            continue
        message = {key: row[key] for key in
                   ("id", "role", "content", "citations_json", "created_at")}
        message["citations"] = json.loads(row["citations_json"] or "[]")
        if is_tool_call:
            message["tool_calls"] = calls
        elif row["role"] == "tool":
            message["tool_call_id"] = row["tool_call_id"]
        elif isinstance(calls, dict):  # 断线补存行的元数据
            message.update(status=calls.get("status"), incomplete=True)
        messages.append(message)
    return messages


def delete_conversation(conversation_id: int) -> bool:
    with connect() as db:
        db.execute("DELETE FROM messages WHERE conversation_id=?", (conversation_id,))
        cursor = db.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
        return cursor.rowcount > 0
