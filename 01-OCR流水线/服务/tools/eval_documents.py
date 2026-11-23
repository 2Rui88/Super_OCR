"""文档识别评测 —— DocumentResult 对照 golden 的量化指标。

指标（证件与文档分开，不用同一把尺子）：
- 字级准确率：整页文本归一化（去空白）后 SequenceMatcher 相似度；
- 表格单元格匹配率：golden 表格与识别表格按序配对，单元格精确相等比例；
- 表格行列结构正确率：行列数完全一致的表格占比；
- 页面方式判定准确率：source_mode 与 golden 标注一致的比例；
- 每页耗时。

golden JSON 结构：
{"file": "...", "provenance": "native_text_layer | ai_vision_transcript",
 "pages": [{"page_num": 1, "mode": "text_layer|ocr", "text": "整页纯文本",
            "tables": [["表头1","表头2"], ["a","b"]]}]}

用法：
    python tools/eval_documents.py --result <任务结果.json> --golden <golden.json> \
        --out tests/_out/文档评测-xxx.json
"""
from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _normalize(text: str) -> str:
    return "".join(str(text or "").split())


def _page_texts(page: dict) -> str:
    blocks = page.get("blocks", [])
    return "\n".join(b.get("text", "") for b in blocks)


def _match_tables(golden_tables: list, extracted_tables: list) -> dict:
    matched = struct_ok = 0
    total_cells = hit_cells = 0
    for index, golden in enumerate(golden_tables or []):
        extracted = extracted_tables[index] if index < len(extracted_tables) else []
        golden_cells = [c for row in golden for c in row]
        extracted_cells = [c for row in extracted for c in row]
        if len(golden) == len(extracted) and all(
                len(a) == len(b) for a, b in zip(golden, extracted)):
            struct_ok += 1
            matched += 1
            total_cells += len(golden_cells)
            hit_cells += sum(1 for a, b in zip(golden_cells, extracted_cells)
                             if _normalize(a) == _normalize(b))
        else:
            total_cells += len(golden_cells)
            hit_cells += sum(1 for a, b in zip(golden_cells, extracted_cells)
                             if _normalize(a) == _normalize(b))
    return {"golden_tables": len(golden_tables or []), "extracted_tables": len(extracted_tables),
            "cell_match": hit_cells, "cell_total": total_cells,
            "cell_match_rate": round(hit_cells / total_cells, 4) if total_cells else None,
            "structure_ok": struct_ok,
            "structure_rate": round(struct_ok / len(golden_tables), 4)
            if golden_tables else None}


def evaluate_document(result: dict, golden: dict) -> dict:
    pages = {p["page_num"]: p for p in result.get("pages", [])}
    page_reports = []
    for expected in golden["pages"]:
        number = expected["page_num"]
        page = pages.get(number)
        if page is None:
            page_reports.append({"page": number, "missing": True})
            continue
        golden_text = _normalize(expected.get("text", ""))
        extracted_text = _normalize(_page_texts(page))
        accuracy = (difflib.SequenceMatcher(None, golden_text, extracted_text).ratio()
                    if golden_text or extracted_text else 1.0)
        extracted_tables = [_parse_html_rows(block["html"]) for block in page.get("blocks", [])
                            if block.get("type") == "table" and block.get("html")]
        tables = _match_tables(expected.get("tables"), extracted_tables)
        page_reports.append({
            "page": number, "char_accuracy": round(accuracy, 4),
            "mode": page.get("source_mode"), "mode_expected": expected.get("mode"),
            "mode_correct": page.get("source_mode") == expected.get("mode"),
            "duration_ms": page.get("duration_ms"), **tables,
        })

    valid = [p for p in page_reports if not p.get("missing")]
    accuracies = [p["char_accuracy"] for p in valid]
    cell_total = sum(p["cell_total"] for p in valid)
    cell_match = sum(p["cell_match"] for p in valid)
    golden_tables = sum(p["golden_tables"] for p in valid)
    struct_ok = sum(p["structure_ok"] for p in valid)
    return {
        "file": golden.get("file"), "provenance": golden.get("provenance"),
        "pages": page_reports,
        "char_accuracy_mean": round(sum(accuracies) / len(accuracies), 4) if accuracies else None,
        "cell_match_rate": round(cell_match / cell_total, 4) if cell_total else None,
        "table_structure_rate": round(struct_ok / golden_tables, 4) if golden_tables else None,
        "mode_accuracy": round(sum(1 for p in valid if p.get("mode_correct")) / len(valid), 4)
        if valid else None,
        "duration_ms_total": sum(p.get("duration_ms") or 0 for p in valid),
    }


def _parse_html_rows(html: str) -> list[list[str]]:
    """从表格 HTML 提取行列（与 _Rows 解析同构，评测侧独立实现避免共享 bug）。"""
    from html.parser import HTMLParser

    class Rows(HTMLParser):
        def __init__(self):
            super().__init__()
            self.rows, self.row, self.cell = [], [], None

        def handle_starttag(self, tag, attrs):
            if tag == "tr":
                self.row = []
            elif tag in ("td", "th"):
                self.cell = []

        def handle_data(self, data):
            if self.cell is not None:
                self.cell.append(data)

        def handle_endtag(self, tag):
            if tag in ("td", "th") and self.cell is not None:
                self.row.append("".join(self.cell))
                self.cell = None
            elif tag == "tr":
                self.rows.append(self.row)

    parser = Rows()
    parser.feed(html)
    return parser.rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, required=True,
                        help="任务结果 JSON（DocumentResult 或含 result 字段的信封）")
    parser.add_argument("--golden", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    result = json.loads(args.result.read_text(encoding="utf-8"))
    if result.get("kind") == "document" or ("result" in result
                                            and "pages" in result.get("result", {})):
        result = result.get("result", result)
    golden = json.loads(args.golden.read_text(encoding="utf-8"))
    report = evaluate_document(result, golden)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{report['file']}：字级准确率 {report['char_accuracy_mean']}"
          f"｜单元格匹配 {report['cell_match_rate']}｜结构 {report['table_structure_rate']}"
          f"｜方式判定 {report['mode_accuracy']}｜总耗时 {report['duration_ms_total']}ms")
    print(f"报告 → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
