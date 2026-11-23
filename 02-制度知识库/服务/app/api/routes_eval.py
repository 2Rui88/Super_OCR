"""评测接口 —— CSV 导入、一键运行、进度与明细、运行历史。"""
from __future__ import annotations

import csv
import io
import json
import time

from fastapi import APIRouter, File, HTTPException, UploadFile

from app import evalservice
from app.db import connect

router = APIRouter(tags=["eval"])

# CSV 列：category,question,gold_answer,gold_files（多文件用分号分隔）
_CATEGORIES = {"正文", "表格", "流程图", "查无", "新旧版", "多轮"}


@router.post("/eval/items/import")
async def import_items(file: UploadFile = File(...)):
    """CSV 格式：category,question,gold_answer,gold_files（覆盖式导入）。"""
    raw = (await file.read()).decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(raw))
    items, errors = [], []
    for i, row in enumerate(reader, 2):
        cat = (row.get("category") or "").strip()
        q = (row.get("question") or "").strip()
        if cat not in _CATEGORIES:
            errors.append(f"第{i}行：题型必须是 {'/'.join(_CATEGORIES)}")
            continue
        if not q:
            errors.append(f"第{i}行：问题为空")
            continue
        gold_files = [x.strip() for x in (row.get("gold_files") or "").split(";")
                      if x.strip()]
        if cat != "查无" and not gold_files:
            errors.append(f"第{i}行：非查无题必须有金标文件")
            continue
        items.append((cat, q, (row.get("gold_answer") or "").strip(),
                      json.dumps(gold_files, ensure_ascii=False)))
    if errors and not items:
        raise HTTPException(422, "; ".join(errors[:5]))
    with connect() as db:
        db.execute("DELETE FROM eval_items")
        db.executemany(
            "INSERT INTO eval_items (category, question, gold_answer, gold_files,"
            " created_at) VALUES (?,?,?,?,?)",
            [(c, q, a, g, int(time.time())) for c, q, a, g in items])
    return {"imported": len(items), "errors": errors}


@router.get("/eval/items")
def list_items():
    with connect() as db:
        rows = db.execute("SELECT * FROM eval_items ORDER BY id").fetchall()
        return {"items": [{**dict(r), "gold_files": json.loads(r["gold_files"])}
                          for r in rows]}


@router.post("/eval/run")
async def start_eval():
    """一键运行：后台逐题执行，进度轮询 /eval/runs/{id}。"""
    with connect() as db:
        total = db.execute("SELECT COUNT(*) c FROM eval_items").fetchone()["c"]
        if total == 0:
            raise HTTPException(422, "评测集为空，请先导入 CSV")
        cur = db.execute("INSERT INTO eval_runs (started_at) VALUES (?)",
                         (int(time.time()),))
        run_id = cur.lastrowid
    evalservice.start_run(run_id)
    return {"run_id": run_id, "total": total}


@router.get("/eval/runs")
def list_runs():
    with connect() as db:
        rows = db.execute(
            "SELECT id, started_at, finished_at, config_json, summary_json"
            " FROM eval_runs ORDER BY id DESC LIMIT 20").fetchall()
        return {"runs": [{**dict(r),
                          "summary": json.loads(r["summary_json"])
                          if r["summary_json"] else None}
                         for r in rows]}


@router.get("/eval/runs/{run_id}")
def run_detail(run_id: int):
    with connect() as db:
        run = db.execute("SELECT * FROM eval_runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            raise HTTPException(404, "运行不存在")
        done = db.execute(
            "SELECT COUNT(*) c FROM eval_results WHERE run_id=?", (run_id,)).fetchone()["c"]
        total = db.execute("SELECT COUNT(*) c FROM eval_items").fetchone()["c"]
        rows = db.execute(
            "SELECT r.*, e.category, e.question, e.gold_answer, e.gold_files"
            " FROM eval_results r JOIN eval_items e ON e.id=r.item_id"
            " WHERE r.run_id=? ORDER BY r.id", (run_id,)).fetchall()
    return {
        "run_id": run_id, "progress": f"{done}/{total}",
        "finished": run["finished_at"] is not None,
        "active": evalservice.run_active(run_id),
        "summary": json.loads(run["summary_json"]) if run["summary_json"] else None,
        "config": json.loads(run["config_json"] or "{}"),
        "results": [{**dict(r), "gold_files": json.loads(r["gold_files"]),
                     "detail": json.loads(r["detail_json"] or "{}")}
                    for r in rows],
    }
