"""评测 —— 预设测试集一键评测 + 上传标准答案对指定批次评测。"""
from __future__ import annotations

import hashlib
import json
import time

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel

from app.core.evaluation import evaluate_batch

router = APIRouter(tags=["eval"])


@router.get("/eval/presets")
def list_presets(request: Request):
    """预设测试集：目录内含图片 + 标准答案.json 即可列出（复用试跑源配置）。"""
    settings = request.app.state.settings
    presets = []
    for label, path in settings.trial_source_dirs:
        golden = _load_preset_golden(path)
        if golden is None:
            continue
        presets.append({"label": label, "files": len(golden), "path": path})
    return {"presets": presets}


def _load_preset_golden(path: str) -> list[dict] | None:
    from pathlib import Path
    golden_path = Path(path) / "标准答案.json"
    if not golden_path.is_file():
        return None
    try:
        data = json.loads(golden_path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) and data else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


class PresetBody(BaseModel):
    label: str


@router.post("/eval/run-preset")
def run_preset(body: PresetBody, request: Request):
    """一键评测：预设目录建证件批次 → 等 worker 跑完 → 对照自带标准答案出报告。

    同步等待（超时 300 秒）；图片内容命中生产缓存时秒级完成。
    """
    store = request.app.state.store
    settings = request.app.state.settings
    source = next((p for label, p in settings.trial_source_dirs
                   if label == body.label), None)
    if source is None:
        raise HTTPException(404, f"未知预设 {body.label}")
    golden = _load_preset_golden(source)
    if golden is None:
        raise HTTPException(422, f"{body.label} 缺少可用的 标准答案.json")

    from app.api.uploads import IMAGE_EXTENSIONS, save_upload
    from pathlib import Path

    src = Path(source)
    task_ids = []
    for entry in sorted(golden, key=lambda g: g["file"]):
        file_src = src / entry["file"]
        if not file_src.is_file():
            raise HTTPException(422, f"预设图片缺失：{entry['file']}")
        digest, path = save_upload(file_src.read_bytes(), entry["file"],
                                   allowed=IMAGE_EXTENSIONS,
                                   upload_dir=settings.upload_dir,
                                   max_mb=settings.max_upload_mb)
        task_ids.append(store.create_task(
            task_type="card", filename=entry["file"], file_sha256=digest,
            file_path=path, hint_type=None))
    batch_id = store.create_batch(task_ids)

    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        batch = store.get_batch(batch_id)
        if batch["counts"]["done"] + batch["counts"]["failed"] >= batch["total"]:
            break
        time.sleep(1.0)
    else:
        raise HTTPException(504, "评测批次 300 秒未完成，稍后可用 /eval 手动重跑")

    report = evaluate_batch(store.batch_tasks(batch_id), golden)
    gt_sha256 = hashlib.sha256(
        json.dumps(golden, ensure_ascii=False).encode()).hexdigest()
    eval_id = store.save_eval_run(batch_id, gt_sha256,
                                  json.dumps(report, ensure_ascii=False))
    return {"eval_id": eval_id, "batch_id": batch_id,
            "preset": body.label, "ground_truth_sha256": gt_sha256,
            "report": report}


@router.post("/eval")
async def evaluate(request: Request, batch_id: int = Form(...),
                   ground_truth: UploadFile = File(...)):
    store = request.app.state.store
    if store.get_batch(batch_id) is None:
        raise HTTPException(status_code=404, detail={
            "error_code": "BATCH_NOT_FOUND", "message": f"批次 {batch_id} 不存在"})
    content = await ground_truth.read()
    try:
        truth = json.loads(content.decode("utf-8"))
        if not isinstance(truth, list) or not truth:
            raise ValueError("标准答案应为非空 JSON 数组")
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail={
            "error_code": "INVALID_GROUND_TRUTH", "message": f"标准答案解析失败：{exc}"})

    report = evaluate_batch(store.batch_tasks(batch_id), truth)
    gt_sha256 = hashlib.sha256(content).hexdigest()
    eval_id = store.save_eval_run(batch_id, gt_sha256,
                                  json.dumps(report, ensure_ascii=False))
    return {"eval_id": eval_id, "batch_id": batch_id,
            "ground_truth_sha256": gt_sha256, "report": report}
