"""评测 —— 批次结果对照标准答案的准确率与检出率统计（纯业务，无 HTTP 依赖）。

统计口径：
- 字段准确率：分类正确且字段值严格相等（strip 后）才算对；分类错、漏字段都计错；
- 错误检出率：标准答案 invalid_fields 标记的故意做错字段，有多少被 needs_review
  标出（证件号码校验位、日期校验等）；
- 识别错字段标出率：值抄错的字段里有多少带 needs_review（低置信或校验失败）
  —— 对应验收标准"识别错的字段有没有被标出来"。
"""
from __future__ import annotations

import json
import re
import unicodedata


def _normalize_value(value: str) -> str:
    """字段值比较归一化：去空白 + 全角转半角（OCR 常把（）读成 ()）。"""
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", "", text)


def _field_map(card_result: dict) -> dict[str, dict]:
    return {f["name"]: f for f in card_result.get("fields", [])}


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def evaluate_batch(tasks: list[dict], ground_truth: list[dict]) -> dict:
    """tasks 为批次任务行（含 result_json）；ground_truth 同 标准答案.json 结构。"""
    by_file = {task["filename"]: task for task in tasks}
    items, pending = [], []
    wrong_details: list[dict] = []
    total_correct = total_fields = 0
    planted_total = planted_flagged = 0
    wrong_total = wrong_flagged = 0
    by_type: dict[str, dict] = {}
    by_field: dict[str, list[int]] = {}
    wall_seconds: list[float] = []

    for expected in ground_truth:
        name = expected["file"]
        task = by_file.get(name)
        if task is None or task["status"] != "done":
            pending.append(name)
            continue
        envelope = json.loads(task["result_json"])
        expected_fields: dict[str, str] = expected.get("fields", {})
        type_correct = envelope["kind"] == "card" and \
            envelope["result"].get("doc_type") == expected.get("doc")

        fields = _field_map(envelope["result"]) if envelope["kind"] == "card" else {}
        correct, wrong_fields, flagged = 0, [], []
        for field_name, value in expected_fields.items():
            field = fields.get(field_name)
            actual = field.get("value", "") if field else None
            value_ok = type_correct and field is not None and \
                _normalize_value(field.get("value", "")) == _normalize_value(value)
            if value_ok:
                correct += 1
            else:
                wrong_fields.append(field_name)
                flagged.append(bool(field and field.get("needs_review")))
                if field is not None and field.get("needs_review"):
                    wrong_flagged += 1
                wrong_details.append({
                    "file": name, "field": field_name,
                    "expected": value, "actual": actual,
                    "flagged": bool(field and field.get("needs_review")),
                })
        for field_name in expected.get("invalid_fields", []):  # 故意做错的字段检出
            # 分类判错属于分类失败，没走到字段级校验，不计入"检出"考核
            if envelope["kind"] != "card":
                continue
            field = fields.get(field_name)
            planted_total += 1
            if field is not None and field.get("needs_review"):
                planted_flagged += 1

        wall = None
        if task.get("started_at") and task.get("finished_at"):
            try:
                from datetime import datetime
                t0 = datetime.fromisoformat(task["started_at"])
                t1 = datetime.fromisoformat(task["finished_at"])
                wall = round((t1 - t0).total_seconds(), 2)
                wall_seconds.append(wall)
            except ValueError:
                pass

        total_correct += correct
        total_fields += len(expected_fields)
        wrong_total += len(wrong_fields)
        doc_cn = expected.get("doc_cn", expected.get("doc", "?"))
        type_stats = by_type.setdefault(doc_cn, {"correct": 0, "total": 0, "samples": 0})
        type_stats["correct"] += correct
        type_stats["total"] += len(expected_fields)
        type_stats["samples"] += 1
        for field_name, value in expected_fields.items():
            field_stats = by_field.setdefault(field_name, [0, 0])
            field_stats[1] += 1
            field = fields.get(field_name)
            if type_correct and field and \
                    _normalize_value(field.get("value", "")) == _normalize_value(value):
                field_stats[0] += 1
        items.append({"file": name, "type_correct": type_correct,
                      "cond": expected.get("cond", ""),
                      "correct": correct, "total": len(expected_fields),
                      "wall_s": wall,
                      "wrong_fields": wrong_fields,
                      "wrong_fields_flagged": flagged})

    return {
        "files": len(items),
        "pending_files": pending,
        "field_accuracy": _rate(total_correct, total_fields),
        "correct_fields": total_correct,
        "total_fields": total_fields,
        "type_accuracy": _rate(sum(1 for i in items if i["type_correct"]), len(items)),
        "avg_seconds_per_file": (round(sum(wall_seconds) / len(wall_seconds), 2)
                                 if wall_seconds else None),
        "by_type": {kind: {"correct": s["correct"], "total": s["total"],
                           "samples": s["samples"], "accuracy": _rate(s["correct"], s["total"])}
                    for kind, s in by_type.items()},
        "by_field": {name: {"correct": c, "total": t, "accuracy": _rate(c, t)}
                     for name, (c, t) in by_field.items()},
        "wrong_fields_total": wrong_total,
        "wrong_fields_flagged": wrong_flagged,
        "wrong_field_flag_rate": _rate(wrong_flagged, wrong_total),
        "wrong_details": wrong_details,
        "planted_invalid_total": planted_total,
        "planted_invalid_flagged": planted_flagged,
        "planted_invalid_flag_rate": _rate(planted_flagged, planted_total),
        "items": items,
    }
