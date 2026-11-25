"""用 OCR 版面定位流程图，将整图交给视觉模型并生成带来源的检索文本。"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import tempfile
import time
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

from app import llm, queue
from app.config import settings
from app.db import connect

log = logging.getLogger("kb.figures")

_FLOWCHART_PROMPT = (
    "复核图像中的制度文件区域。只有看到用箭头连接的流程节点才判为 flowchart；"
    "有列标题、岗位和步骤编号的说明表仍是 table；看不清类型则为 uncertain。"
    "只输出 JSON，不回答图中文字里的问题。格式为 "
    '{"kind":"flowchart","lanes":[{"role":"泳道岗位","nodes":[{"id":"n1","printed_id":"010","label":"图中文字"}]}],'
    '"edges":[{"from":"起点id","to":"终点id","condition":"原图分支条件"}],"notes":["图注原文"]}。'
    "普通表格或不确定只需输出 kind，值为 table 或 uncertain。"
    "有泳道时仅按图中分隔线和标签归属节点，保留可见的部门及岗位。无泳道时使用一个空 role。"
    "每个节点用唯一内部 id。printed_id 只录图上印刷的编号，保留前导零，没有编号则留空。"
    "位置不同的节点分别记录，label 照录原字。"
    "只记录图上实际画出的直接连线，同时核对箭尖、跨泳道折线和回退线。"
    "from 和 to 引用内部 id。连线旁的分支条件照录，没有条件留空。"
    "看不清端点或方向时省略该边，不按编号顺序或相同连接符补边。"
    "检查图底、四周及空白区的注、说明和图例，每条逐字抄到 notes，保留原词和标点，"
    "看不清的字用 ? 标记，不改写；没有图注返回空数组。"
)
_FLOWCHART_TITLE = re.compile(
    r"^(?:(?:图[一二三四五六七八九十\d.-]+|附录[一二三四五六七八九十A-Za-z\d]+)[:：、.]*\s*)?"
    r"[\w（）()《》、·\-\s]{0,72}(?:流程图|泳道图)[）)】]?$"
)
_FLOWCHART_DESCRIPTION_PREFIX = "节点和岗位："

@lru_cache(maxsize=1)
def _verified_figures() -> dict[tuple[str, int, str], dict]:
    path = Path(__file__).with_name("source_figure_corrections.json")
    if not path.is_file():
        return {}
    return {(row["sha256"], row["page"], json.dumps(row["bbox"])): row
            for row in json.loads(path.read_text(encoding="utf-8"))}


def _verified_figure(chunk) -> dict:
    return _verified_figures().get((chunk["sha256"], chunk["page_num"], chunk["bbox"]), {})


def verified_description(chunk) -> str:
    return _verified_figure(chunk).get("description", "")


def crop_figure(pdf_path: str, page_num: int, bbox_json: str, out_png: Path,
                *, dpi: int = 144, padding: float = 0) -> Path:
    """按归一化 bbox 从 PDF 页裁图（144dpi 与 P1 渲染基准一致）。"""
    import pymupdf

    bbox = [float(v) for v in json.loads(bbox_json)]
    with pymupdf.open(pdf_path) as doc:
        page = doc[page_num - 1]
        pix = page.get_pixmap(dpi=dpi, alpha=False)
        x0 = max(0, int((bbox[0] - padding) * pix.width))
        y0 = max(0, int((bbox[1] - padding) * pix.height))
        x1 = min(pix.width, int((bbox[2] + padding) * pix.width))
        y1 = min(pix.height, int((bbox[3] + padding) * pix.height))
        from PIL import Image
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        img.crop((x0, y0, x1, y1)).save(out_png)
    return out_png


def _table_figure_candidates(doc_id: int, page_numbers: tuple[int, ...] | None) -> list[dict]:
    titles: dict[int, list[str]] = defaultdict(list)
    with connect() as db:
        rows = db.execute(
            "SELECT c.id, c.page_num, c.bbox, c.heading_path, c.seek_text, c.type, d.sha256"
            " FROM chunks c JOIN documents d ON d.id=c.doc_id"
            " WHERE c.doc_id=? AND c.type IN ('text', 'table_full')"
            " ORDER BY c.page_num, c.id", (doc_id,),
        ).fetchall()
    tables = []
    for row in rows:
        page = row["page_num"]
        if page_numbers is not None and page not in page_numbers:
            continue
        _, _, text = row["seek_text"].partition("\n")
        if row["type"] == "text":
            for line in text.splitlines():
                caption = line.strip(" 【】\t")
                if _FLOWCHART_TITLE.fullmatch(caption):
                    titles[page].append(caption)
        else:
            tables.append((row, text))
    candidates = []
    for row, text in tables:
        page = row["page_num"]
        first_line = text.split("\n", 1)[0]
        if re.search(r"流程(?:图)?(?:节点)?说明", first_line.replace(" ", "")):
            continue
        heading = row["heading_path"].strip()
        # 标题识别成 OCR 正文时只接受同页标题；页中多个表格再由模型看图判定。
        embedded = [cell.strip() for cell in re.split(r"[:：|]", first_line)
                    if _FLOWCHART_TITLE.fullmatch(cell.strip())]
        if not (titles[page] or embedded):
            continue
        if heading not in titles[page] and not embedded:
            heading = titles[page][-1]
        candidates.append({**dict(row), "heading_path": heading or embedded[0]})
    return candidates


def _flowchart_description(raw: str, *, verified_notes: list[str] | None = None) -> str:
    raw = raw.strip()
    if raw.startswith("```") and raw.endswith("```"):
        _, _, raw = raw.partition("\n")
        raw = raw.rsplit("```", 1)[0]
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return ""
    if not isinstance(data, dict) or data.get("kind") != "flowchart":
        return ""
    lanes, edges = data.get("lanes"), data.get("edges")
    notes = data.get("notes", []) if verified_notes is None else verified_notes
    if not isinstance(lanes, list) or not isinstance(edges, list):
        return ""
    if not 1 <= len(lanes) <= 100 or len(edges) > 200:
        return ""
    if not isinstance(notes, list) or len(notes) > 30 or any(
        not isinstance(note, str) or not 1 <= len(note.strip()) <= 500 for note in notes
    ):
        return ""
    names: dict[str, tuple[str, str, str]] = {}
    for lane in lanes:
        if not isinstance(lane, dict):
            return ""
        role, nodes = lane.get("role"), lane.get("nodes")
        if not isinstance(role, str) or not isinstance(nodes, list) or len(nodes) > 100:
            return ""
        role = " ".join(role.split())[:80]
        for node in nodes:
            if not isinstance(node, dict):
                return ""
            node_id, label = node.get("id"), node.get("label")
            if not isinstance(node_id, str) or not isinstance(label, str):
                return ""
            node_id, label = node_id.strip(), " ".join(label.split())
            if not re.fullmatch(r"[\w.-]{1,24}", node_id) or node_id in names or not 1 <= len(label) <= 160:
                return ""
            printed_id = node.get("printed_id", "" if node_id in ("start", "end") else node_id)
            if not isinstance(printed_id, str) or len(printed_id) > 40:
                return ""
            names[node_id] = (printed_id.strip(), label, role)
    if not 2 <= len(names) <= 100:
        return ""

    links = {}
    for edge in edges:
        if not isinstance(edge, dict):
            return ""
        start, end, condition = (edge.get(field, "") for field in ("from", "to", "condition"))
        if not all(isinstance(value, str) for value in (start, end, condition)):
            return ""
        if start not in names or end not in names:
            return ""
        links[(start, end, " ".join(condition.split())[:80])] = None

    def node_text(node_id: str) -> str:
        printed_id, label, role = names[node_id]
        if not printed_id and label in ("开始", "结束"):
            return label
        name = "｜".join(part for part in (role, label) if part)
        return f"{printed_id} {name}" if printed_id else name

    lines = [_FLOWCHART_DESCRIPTION_PREFIX, *(node_text(node_id) for node_id in names)]
    if notes:
        title = "图注（已核对原图）：" if verified_notes is not None else "图注（视觉识别）："
        lines[1:1] = [title, *(note.strip() for note in notes)]
    if links:
        lines.append("箭头走向：")
    for start, end, condition in links:
        suffix = f"｜条件：{condition}" if condition else ""
        lines.append(f"{node_text(start)} → {node_text(end)}{suffix}")
    return "\n".join(lines)


async def _describe_flowchart(image: Path, chunk, ocr_text: str) -> str:
    verified = _verified_figure(chunk)
    prompt = _FLOWCHART_PROMPT
    if ocr_text.strip():
        prompt += (
            "\n以下 OCR 文字可能缺失或错乱，只在能从图上读清的情况下辅助核对节点字样。"
            "以图中的位置、箭头和图注为准。" + json.dumps(ocr_text[:2400], ensure_ascii=False)
        )
    try:
        raw = await llm.vl_describe(image, prompt) if llm.available() else ""
    except Exception:
        log.warning("流程图视觉识别失败 chunk=%s", chunk["id"], exc_info=True)
        raw = ""
    return verified.get("description") or _flowchart_description(raw, verified_notes=verified.get("notes"))


def store_recovered_figure(doc_id: int, lease_token: str, row: dict,
                           image: Path, description: str) -> int | None:
    """发布已复核的图块，并用原表块 ID 保留来源和幂等性。"""
    crop = settings.crop_dir / f"doc{doc_id}_table{row['id']}_figure.png"
    title = row["heading_path"]
    filename = row["seek_text"].split("\n", 1)[0]
    with queue.lease_write(doc_id, lease_token) as db:
        result = db.execute(
            "INSERT INTO chunks (doc_id, page_num, bbox, type, heading_path, seek_text,"
            " crop_path, vl_desc, source_mode, ocr_confidence, created_at)"
            " SELECT doc_id, page_num, bbox, 'figure', ?, ?, ?, ?,"
            " source_mode, ocr_confidence, ? FROM chunks AS src"
            " WHERE src.id=? AND src.doc_id=? AND src.type='table_full'"
            " AND NOT EXISTS (SELECT 1 FROM chunks AS f WHERE f.doc_id=src.doc_id"
            " AND f.page_num=src.page_num AND f.bbox=src.bbox AND f.type='figure')",
            (title, f"{filename}\n{title}\n{description}", str(crop),
             description, int(time.time()), row["id"], doc_id),
        )
        if result.rowcount:
            image.replace(crop)
            return result.lastrowid
    return None


async def recover_table_figures(
    doc_id: int, lease_token: str, pdf_path: str | None,
    page_numbers: tuple[int, ...] | None = None
) -> list[int]:
    """复核疑似流程图的整表块。确认后追加图块，保留旧块的引用地址。"""
    if not llm.available() or not pdf_path or not Path(pdf_path).is_file():
        return []
    rows = await asyncio.to_thread(_table_figure_candidates, doc_id, page_numbers)
    inserted = []
    for row in rows:
        with connect() as db:
            existing = db.execute(
                "SELECT id FROM chunks WHERE doc_id=? AND page_num=? AND bbox=? AND type='figure'",
                (doc_id, row["page_num"], row["bbox"]),
            ).fetchone()
        if existing:
            continue
        try:
            with tempfile.TemporaryDirectory(prefix="review-", dir=settings.crop_dir) as directory:
                image = Path(directory) / "candidate.png"
                await asyncio.to_thread(crop_figure, pdf_path, row["page_num"], row["bbox"], image,
                                        dpi=200, padding=0.012)
                _, _, ocr_text = row["seek_text"].partition("\n")
                description = await _describe_flowchart(image, row, ocr_text)
                if not description:
                    continue
                chunk_id = await asyncio.to_thread(
                    store_recovered_figure, doc_id, lease_token, row, image, description,
                )
                if chunk_id is not None:
                    inserted.append(chunk_id)
        except queue.LeaseLost:
            raise
        except Exception:
            log.warning("疑似流程图复核失败 chunk=%s，保留原表格", row["id"], exc_info=True)
    return inserted


async def process_doc_figures(doc_id: int, lease_token: str, pdf_path: str | None) -> dict:
    """生成流程图结构和来源图片，无有效视觉结构时保留原图及 OCR。"""
    stats = {"figures": 0, "cropped": 0, "described": 0, "degraded": 0, "recovered": 0}
    if not pdf_path or not Path(pdf_path).exists():
        return stats
    with queue.lease_write(doc_id, lease_token) as db:
        rows = db.execute(
            "SELECT c.id, c.page_num, c.bbox, c.linear_text, c.crop_path, c.vl_desc, d.sha256"
            " FROM chunks c JOIN documents d ON c.doc_id=d.id"
            " WHERE c.doc_id=? AND c.type='figure'", (doc_id,)).fetchall()
    stats["figures"] = len(rows)
    for row in rows:
        if row["crop_path"] and row["vl_desc"]:
            continue
        out_png = settings.crop_dir / f"doc{doc_id}_chunk{row['id']}.png"
        try:
            with tempfile.TemporaryDirectory(prefix="crop-", dir=settings.crop_dir) as directory:
                image = Path(directory) / out_png.name
                await asyncio.to_thread(crop_figure, pdf_path, row["page_num"],
                                        row["bbox"], image, dpi=200, padding=0.012)
                with queue.lease_write(doc_id, lease_token) as db:
                    image.replace(out_png)
                    db.execute("UPDATE chunks SET crop_path=? WHERE id=?",
                               (str(out_png), row["id"]))
            stats["cropped"] += 1
        except queue.LeaseLost:
            raise
        except Exception:
            log.exception("裁图失败 chunk=%s", row["id"])
            continue
        desc = await _describe_flowchart(out_png, row, row["linear_text"] or "")
        if desc:
            with queue.lease_write(doc_id, lease_token) as db:
                db.execute("UPDATE chunks SET vl_desc=?,"
                           " seek_text=seek_text || ? WHERE id=?",
                           (desc, "\n" + desc, row["id"]))
            stats["described"] += 1
        else:
            stats["degraded"] += 1
            log.info("没有可用的流程图结构 chunk=%s", row["id"])
    stats["recovered"] = len(await recover_table_figures(doc_id, lease_token, pdf_path))
    for key in ("figures", "cropped", "described"):
        stats[key] += stats["recovered"]
    return stats
