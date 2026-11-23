"""证件配置版本管理 —— DB 为运行时真源，文件目录仅作首次种子。

流程：PUT 草稿（严格校验）→ 创建试跑（双波任务：draft 快照 / 当时生效参照，
各自独立缓存空间）→ 两波完成后对照标准答案出对比报告 → 确认（原子切换
active 版本 + 刷新整套指纹，worker 心跳发现指纹变化即热重建 registry，
证件缓存键含该指纹，旧缓存自然失效）或取消。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.certs.loader import (load_certs_from_texts, registry_hash,
                              validate_cert_text)
from app.core.evaluation import evaluate_batch

TRIAL_TASK_TYPE = "cert_trial_card"
GOLDEN_FILENAME = "标准答案.json"


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- 种子与指纹

def ensure_seeded(store, cert_dir: str | Path) -> None:
    """active 版本不存在时从 config/certs/*.yaml 导入 v1；指纹始终对齐现状。"""
    if not store.active_cert_texts():
        from app.certs.loader import load_certs
        for path in sorted(Path(cert_dir).glob("*.yaml")):
            text = path.read_text(encoding="utf-8")
            cfg = validate_cert_text(text)
            store.insert_cert_version(
                type_id=cfg.type_id, yaml_text=text,
                yaml_sha256=hashlib.sha256(text.encode()).hexdigest(),
                status="active", note="种子导入（config/certs 文件）")
    refresh_active_hash(store)


def refresh_active_hash(store) -> str:
    """重算生效整套指纹并写入 cert_state（worker 心跳据此热切换）。"""
    registry = load_certs_from_texts(store.active_cert_texts())
    digest = registry_hash(registry)
    store.set_cert_state_hash(digest)
    return digest


# ---------------------------------------------------------------- 试跑创建

def load_golden(source_dir: str | Path) -> list[dict]:
    path = Path(source_dir) / GOLDEN_FILENAME
    if not path.is_file():
        raise FileNotFoundError(f"{source_dir} 下没有 {GOLDEN_FILENAME}")
    return json.loads(path.read_text(encoding="utf-8"))


def create_trial(store, *, upload_dir: Path, max_mb: int, type_id: str,
                 draft_version_id: int, draft_yaml: str, source_label: str,
                 source_dir: str | Path, scope: str) -> int:
    """建两波任务：draft（草稿替换该类后的整套）与 ref（当时生效整套）。

    scope = 'all' 全量 | 'type' 仅该证件类（golden 的 doc == type_id）。
    图片从源目录按内容去重落到 uploads，两波共用同一批文件路径。
    """
    from app.api.uploads import IMAGE_EXTENSIONS, save_upload

    limit = max_mb * 1024 * 1024

    golden = load_golden(source_dir)
    if scope == "type":
        golden = [g for g in golden if g.get("doc") == type_id]
    golden = sorted(golden, key=lambda g: g["file"])
    if not golden:
        raise ValueError(f"测试集中没有 doc == {type_id} 的条目")

    saved: list[tuple[str, str, str]] = []  # (filename, sha256, path)
    for entry in golden:
        src = Path(source_dir) / entry["file"]
        if not src.is_file():
            raise FileNotFoundError(f"测试图缺失：{src}")
        content = src.read_bytes()
        if len(content) > limit:
            raise ValueError(f"{entry['file']} 超过 {max_mb}MB 上限")
        digest, path = save_upload(content, entry["file"], allowed=IMAGE_EXTENSIONS,
                                   upload_dir=upload_dir, max_mb=max_mb)
        saved.append((entry["file"], digest, str(path)))

    active_texts = store.active_cert_texts()
    draft_set = dict(active_texts)
    draft_set[type_id] = draft_yaml

    # 试跑行必须先于任务可领取落库：worker 领到任务会立刻反查归属，
    # 行不存在/未挂批次时按"未就绪"重排队，而不是失败
    trial_id = store.insert_cert_trial(
        type_id=type_id, draft_version_id=draft_version_id,
        source=f"{source_label}（{scope}，{len(saved)} 张）",
        draft_set_json=json.dumps(draft_set, ensure_ascii=False),
        ref_set_json=json.dumps(active_texts, ensure_ascii=False),
        draft_batch_id=0, ref_batch_id=0,
        gt_json=json.dumps(golden, ensure_ascii=False), status="running")

    task_ids = {"draft": [], "ref": []}
    for role in ("draft", "ref"):
        for filename, digest, path in saved:
            task_ids[role].append(store.create_task(
                task_type=TRIAL_TASK_TYPE, filename=filename,
                file_sha256=digest, file_path=path, hint_type=None))
    draft_batch = store.create_batch(task_ids["draft"])
    ref_batch = store.create_batch(task_ids["ref"])
    store.update_cert_trial(trial_id, draft_batch_id=draft_batch,
                            ref_batch_id=ref_batch)
    return trial_id


def _wave_counts(store, batch_id: int) -> dict:
    tasks = store.batch_tasks(batch_id)
    counts = {"total": len(tasks), "done": 0, "failed": 0, "pending": 0}
    for task in tasks:
        if task["status"] == "done":
            counts["done"] += 1
        elif task["status"] == "failed":
            counts["failed"] += 1
        else:
            counts["pending"] += 1
    return counts


def trial_progress(store, trial: dict) -> dict:
    return {"draft": _wave_counts(store, trial["draft_batch_id"]),
            "ref": _wave_counts(store, trial["ref_batch_id"])}


def finalize_if_done(store, trial: dict) -> dict | None:
    """两波任务全部到终态时生成报告并把 running → ready（幂等，谁先读到谁收尾）。

    前端轮询的是列表端点，收尾不能只挂在详情端点上——否则任务全部完成后
    列表永远返回"运行中"，页面卡死在"（重跑进行中）"。未完成返回 None。
    """
    progress = trial_progress(store, trial)
    if any(w["pending"] for w in progress.values()):
        return None
    report = build_trial_report(store, trial)
    store.update_cert_trial(trial["id"], status="ready",
                            report_json=json.dumps(report, ensure_ascii=False),
                            finished_at=_now_iso())
    return report


# ---------------------------------------------------------------- 对比报告

def _metrics(report: dict) -> dict:
    return {
        "field_accuracy": report["field_accuracy"],
        "correct_fields": report["correct_fields"],
        "total_fields": report["total_fields"],
        "type_accuracy": report["type_accuracy"],
        "planted_flagged": report["planted_invalid_flagged"],
        "planted_total": report["planted_invalid_total"],
        "wrong_flagged": report["wrong_fields_flagged"],
        "wrong_total": report["wrong_fields_total"],
    }


def _field_rows(entry: dict, draft_task: dict | None, ref_task: dict | None) -> list[dict]:
    """单文件字段级明细：标准答案 × 修改版识别值 × 生效版识别值，逐字段判定。"""
    from app.core.evaluation import _normalize_value

    def field_map(task):
        if not task or task["status"] != "done":
            return {}
        envelope = json.loads(task["result_json"])
        if envelope.get("kind") != "card":
            return {}
        return {f["name"]: f for f in envelope["result"].get("fields", [])}

    dm, rm = field_map(draft_task), field_map(ref_task)
    rows = []
    for name, expected in entry.get("fields", {}).items():
        d, r = dm.get(name), rm.get(name)
        dv = (d or {}).get("value", "")
        rv = (r or {}).get("value", "")
        rows.append({
            "name": name, "expected": expected,
            "draft_value": dv, "draft_ok": _normalize_value(dv) == _normalize_value(expected),
            "ref_value": rv, "ref_ok": _normalize_value(rv) == _normalize_value(expected),
            "draft_review": bool((d or {}).get("needs_review")),
        })
    return rows


def build_trial_report(store, trial: dict) -> dict:
    """两波各对照 golden 评测 + 逐文件对比（含字段级明细，前端按需展开）。"""
    golden = json.loads(trial["gt_json"])
    draft_tasks = store.batch_tasks(trial["draft_batch_id"])
    ref_tasks = store.batch_tasks(trial["ref_batch_id"])
    draft_report = evaluate_batch(draft_tasks, golden)
    ref_report = evaluate_batch(ref_tasks, golden)
    draft_by_file = {item["file"]: item for item in draft_report["items"]}
    ref_by_file = {item["file"]: item for item in ref_report["items"]}
    draft_task_by_file = {t["filename"]: t for t in draft_tasks}
    ref_task_by_file = {t["filename"]: t for t in ref_tasks}

    files, counts = [], {"better": 0, "worse": 0, "same": 0}
    for name in sorted(draft_by_file):
        d, r = draft_by_file[name], ref_by_file.get(name)
        r_correct = r["correct"] if r else -1
        verdict = ("better" if d["correct"] > r_correct
                   else "worse" if d["correct"] < r_correct else "same")
        counts[verdict] += 1
        entry = next((g for g in golden if g["file"] == name), {})
        files.append({
            "file": name,
            "draft": {"type_ok": d["type_correct"], "correct": d["correct"],
                      "total": d["total"], "wrong_fields": d["wrong_fields"]},
            "ref": ({"type_ok": r["type_correct"], "correct": r["correct"],
                     "total": r["total"], "wrong_fields": r["wrong_fields"]}
                    if r else None),
            "verdict": verdict,
            "fields": _field_rows(entry, draft_task_by_file.get(name),
                                  ref_task_by_file.get(name)),
        })

    d_m, r_m = _metrics(draft_report), _metrics(ref_report)
    return {
        "schema": 2,
        "draft": d_m, "ref": r_m,
        "delta": {"field_accuracy": round(d_m["field_accuracy"] - r_m["field_accuracy"], 4),
                  "correct_fields": d_m["correct_fields"] - r_m["correct_fields"],
                  "type_accuracy": round(d_m["type_accuracy"] - r_m["type_accuracy"], 4)},
        "files": files, "file_verdicts": counts,
        "pending_files": draft_report["pending_files"],
    }
