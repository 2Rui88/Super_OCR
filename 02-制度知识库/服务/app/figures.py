"""A6 图加工 —— bbox 本地裁图（144dpi，与 P1 识别同基准）+ VL 结构描述。

交叉校验防幻觉：VL 提到的节点名必须大都能在 OCR linear_text 里找到，
否则判定为幻觉，降级为纯 OCR 文字（vl_desc 留空 + 文档记 warning）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

from app import llm
from app.config import settings
from app.db import connect

log = logging.getLogger("kb.figures")

_VL_PROMPT = (
    "这是一张制度文件中的流程图。请只依据图中的文字与箭头，用简洁中文描述："
    "1) 各节点名称（与图中文字完全一致）；2) 从哪里到哪里、判定条件分支是什么。"
    "不要推断图中没有的规则，不要添加建议。"
)


def crop_figure(pdf_path: str, page_num: int, bbox_json: str, out_png: Path) -> Path:
    """按归一化 bbox 从 PDF 页裁图（144dpi 与 P1 渲染基准一致）。"""
    import pymupdf

    bbox = [float(v) for v in json.loads(bbox_json)]
    with pymupdf.open(pdf_path) as doc:
        page = doc[page_num - 1]
        pix = page.get_pixmap(dpi=144, alpha=False)
        x0 = int(bbox[0] * pix.width)
        y0 = int(bbox[1] * pix.height)
        x1 = int(bbox[2] * pix.width)
        y1 = int(bbox[3] * pix.height)
        from PIL import Image
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        img.crop((x0, y0, x1, y1)).save(out_png)
    return out_png


def _cross_check_ok(vl_desc: str, linear_text: str) -> bool:
    """VL 节点名 ⊆ OCR 文字：提取 VL 里的引号内词与中文短语，
    至少一半能在 linear_text 中找到（去空白后子串匹配）。"""
    if not linear_text:
        return False
    # 中文引号“ ”内的高置信节点名优先
    candidates = re.findall("\u201c([^\u201d]{2,12})\u201d", vl_desc)
    if not candidates:
        candidates = re.findall(r"[\u4e00-\u9fff]{2,8}", vl_desc)
    if not candidates:
        return False
    hay = re.sub(r"\s+", "", linear_text)
    hits = sum(1 for c in candidates if re.sub(r"\s+", "", c) in hay)
    return hits >= max(1, len(candidates) // 2)


async def process_doc_figures(doc_id: int, pdf_path: str | None) -> dict:
    """裁图 + VL 描述 + 交叉校验。无密钥/无 PDF/无图块时安全跳过。"""
    stats = {"figures": 0, "cropped": 0, "described": 0, "degraded": 0}
    if not pdf_path or not Path(pdf_path).exists():
        return stats
    with connect() as db:
        rows = db.execute(
            "SELECT id, page_num, bbox, linear_text FROM chunks"
            " WHERE doc_id=? AND type='figure'", (doc_id,)).fetchall()
    stats["figures"] = len(rows)
    if not rows:
        return stats

    for row in rows:
        out_png = settings.crop_dir / f"doc{doc_id}_chunk{row['id']}.png"
        try:
            await asyncio.to_thread(crop_figure, pdf_path, row["page_num"],
                                     row["bbox"], out_png)
            stats["cropped"] += 1
        except Exception:
            log.exception("裁图失败 chunk=%s", row["id"])
            continue
        with connect() as db:
            db.execute("UPDATE chunks SET crop_path=? WHERE id=?",
                       (str(out_png), row["id"]))
        if not llm.available():
            continue
        try:
            desc = await llm.vl_describe(out_png, _VL_PROMPT)
        except Exception:
            log.warning("VL 描述失败 chunk=%s（降级纯 OCR 文字）", row["id"], exc_info=True)
            stats["degraded"] += 1
            continue
        if _cross_check_ok(desc, row["linear_text"] or ""):  # 锚=OCR 文字（pack 已兜底块 text）
            with connect() as db:
                db.execute("UPDATE chunks SET vl_desc=?,"
                           " seek_text=seek_text || ? WHERE id=?",
                           (desc, "\n" + desc, row["id"]))
            stats["described"] += 1
        else:
            stats["degraded"] += 1
            log.warning("VL 描述未过交叉校验 chunk=%s（疑似幻觉，弃用）", row["id"])
    return stats
