"""批次导出 —— JSON 全量转储 / Excel 人读报表（证件号码按文本写入，防科学计数法）。"""
from __future__ import annotations

import io
import json
from urllib.parse import quote

from fastapi import HTTPException
from fastapi.responses import Response

#: 固定列顺序在前，未列出的字段按字典序追加
_FIELD_ORDER = [
    "姓名", "性别", "民族", "出生日期", "住址", "公民身份号码",
    "签发机关", "有效期限", "证号", "国籍", "初次领证日期", "准驾车型",
    "统一社会信用代码", "名称", "类型", "法定代表人", "经营范围",
    "注册资本", "成立日期", "住所",
]


def export_json(batch: dict, tasks: list[dict]) -> Response:
    payload = {
        "batch_id": batch["batch_id"],
        "created_at": batch["created_at"],
        "counts": batch["counts"],
        "items": [
            {
                "task_id": task["id"],
                "filename": task["filename"],
                "status": task["status"],
                "error_code": task["error_code"],
                "error_message": task["error_message"],
                "result": json.loads(task["result_json"]) if task["result_json"] else None,
            }
            for task in tasks
        ],
    }
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    filename = quote(f"batch-{batch['batch_id']}.json")
    return Response(content=body, media_type="application/json",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"})


def _flat_rows(tasks: list[dict]) -> tuple[list[str], list[list[str]]]:
    """任务行 → (列名, 文本行)。列 = 固定顺序 ∪ 批内出现过的字段。"""
    parsed = []
    field_names: set[str] = set()
    for task in tasks:
        kind, doc_cn, fields, note = "", "", {}, ""
        if task["status"] == "done" and task["result_json"]:
            envelope = json.loads(task["result_json"])
            kind = envelope["kind"]
            result = envelope["result"]
            if kind == "card":
                doc_cn = result.get("doc_type_cn", "")
                fields = {f["name"]: f["value"] for f in result.get("fields", [])}
                issues = result.get("issues") or []
                note = "；".join(issues)
            elif task["task_type"] == "document_scan":
                doc_cn = "文档"
                note = f"共 {len(result.get('pages') or [])} 页"
            else:  # 证件任务路由成文档的兜底行
                doc_cn = "非证件（按文档识别）"
                note = envelope.get("reason", "")
        else:
            doc_cn = ""
            note = task["error_message"] or task["error_code"] or ""
        field_names.update(fields)
        parsed.append((task["filename"], task["status"], doc_cn, fields, note))

    ordered = [name for name in _FIELD_ORDER if name in field_names]
    ordered += sorted(field_names - set(_FIELD_ORDER))
    columns = ["文件名", "证件类型", "状态", *ordered, "问题与提示"]
    rows = []
    for filename, status, doc_cn, fields, note in parsed:
        rows.append([filename, doc_cn, status,
                     *(fields.get(name, "") for name in ordered), note])
    return columns, rows


def export_xlsx(batch: dict, tasks: list[dict]) -> Response:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    columns, rows = _flat_rows(tasks)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "批次结果"
    sheet.append(columns)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.number_format = "@"  # 全列文本格式：证件号码绝不能变科学计数法
    for row in rows:
        sheet.append(row)
        for cell in sheet[sheet.max_row]:
            cell.value = str(cell.value) if cell.value is not None else ""
            cell.number_format = "@"
    sheet.column_dimensions["A"].width = 28
    for index in range(2, len(columns) + 1):
        sheet.column_dimensions[get_column_letter(index)].width = 22

    buffer = io.BytesIO()
    workbook.save(buffer)
    filename = quote(f"batch-{batch['batch_id']}.xlsx")
    return Response(
        content=buffer.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


def export_batch(batch: dict | None, tasks: list[dict], fmt: str) -> Response:
    if batch is None:
        raise HTTPException(status_code=404, detail={
            "error_code": "BATCH_NOT_FOUND", "message": "批次不存在"})
    if fmt == "json":
        return export_json(batch, tasks)
    if fmt == "xlsx":
        return export_xlsx(batch, tasks)
    raise HTTPException(status_code=422, detail={
        "error_code": "UNSUPPORTED_FORMAT", "message": "format 只支持 json 或 xlsx"})
