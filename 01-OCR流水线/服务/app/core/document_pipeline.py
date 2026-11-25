"""统一文档链路：每页渲染成图 → PP-StructureV3，不再区分文字层/扫描页。

统一的原因：text_layer 直读与 OCR 两种路径产出的块粒度/结构不一致，
下游（知识库切块）质量参差不齐；全部走 StructureV3 换来同构的
标题/表格/阅读顺序结构。代价是每页都过模型（GPU 上约秒级/页），
重复提交由 worker 的文档级缓存吸收。
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import logging
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable

from app.contracts import Block, BlockType, DocumentResult, PageResult, SourceMode
from app.engine.paddle_card import mkldnn_validated, runtime_versions

log = logging.getLogger(__name__)

# 只列影响识别输出的开关；cpu_threads 只影响速度，不进签名
# 印章识别 2026-11 GPU 实测开启（tools/ab_seal_test.py，tests/_out/seal_test）：
# 4/4 章检出、每页耗时持平（~3.8s），且关闭时章面文字会混入正文块污染
# 知识库文本——开启后印章独立成 seal 块，不再泄漏。
_STRUCTURE_OPTIONS = dict(
    use_doc_orientation_classify=False, use_doc_unwarping=False,
    use_formula_recognition=False, use_chart_recognition=False,
    use_seal_recognition=True, use_region_detection=False,
    use_table_recognition=True,
)

#: 文档类任务类型（领取互斥与并发上限按这一组统计）。现在文档统一走
#: document_scan；document / document_text_layer 仅为旧任务库遗留行的兼容值。
DOCUMENT_TASK_TYPES = ("document", "document_text_layer", "document_scan")


def document_engine_signature(extra_options: dict | None = None,
                              device: str = "cpu") -> str:
    """PP-StructureV3 输出相关配置 + 运行时版本的签名（缓存键组成部分）。

    device 进签名：CPU 与 GPU 的识别数值有微差，结果缓存必须分离。
    """
    payload = dict(
        schema=3, pipeline="PPStructureV3", device=device,
        options={**_STRUCTURE_OPTIONS, **(extra_options or {})},
        runtime=runtime_versions(),
    )
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _bbox(box, width, height):
    return [max(0.0, min(1.0, float(value)/size))
            for value, size in zip(box, (width, height, width, height))]


def _shadow_rows(rows):
    """StructureV3 表格行 → 带表头的影子文本（进 Block.text，供检索与切块）。"""
    if not rows:
        return []
    headers = [str(value or f"列{index+1}") for index, value in enumerate(rows[0])]
    shadows = [" | ".join(f"{headers[index] if index < len(headers) else index+1}: {value or ''}"
                          for index, value in enumerate(row)) for row in rows[1:]]
    # 跨页表头只有一行时，表头本身作为影子文本，保证 text 非空
    return shadows or [" | ".join(headers)]


class _Rows(HTMLParser):
    def __init__(self):
        super().__init__(); self.rows = []; self.row = []; self.cell = None
    def handle_starttag(self, tag, attrs):
        if tag == "tr": self.row = []
        elif tag in ("td", "th"): self.cell = []
        elif tag == "br" and self.cell is not None: self.cell.append("\n")
    def handle_data(self, data):
        if self.cell is not None: self.cell.append(data)
    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None:
            self.row.append("".join(self.cell)); self.cell = None
        elif tag == "tr": self.rows.append(self.row)


def _layout_blocks(result):
    for item in result.get("parsing_res_list", []):
        if isinstance(item, dict):
            box = item.get("block_bbox")
            if box is not None:
                yield item.get("block_label"), item.get("block_content", ""), tuple(box)
        elif item.bbox is not None:
            yield item.label, item.content, tuple(item.bbox)


def _contains_center(region, box):
    x0, y0, x1, y1 = region
    left, top, right, bottom = map(float, box)
    return x0 <= (left + right) / 2 <= x1 and y0 <= (top + bottom) / 2 <= y1


def _table_recheck_boxes(result):
    """表结构像 180° 倒转、表内文字却大多正向时，仅复读该表。"""
    ocr = result.get("overall_ocr_res", {})
    angles = ocr.get("textline_orientation_angles", ())
    boxes = ocr.get("rec_boxes", ())
    scores = ocr.get("rec_scores", ())
    texts = ocr.get("rec_texts", ())
    if not (len(angles) == len(boxes) == len(scores) == len(texts)):
        return set()
    # 方向标签跟检测框对齐，识别结果若过滤了文本就无法安全地按位置对应。
    if len(ocr.get("dt_polys", angles)) != len(angles):
        return set()

    reversed_tables = []
    for table in result.get("table_res_list", []):
        cells = table.get("cell_box_list", ())
        if len(cells) < 4:
            continue
        first, last = cells[0], cells[-1]
        # 首格在右下、末格在左上。90°/270° 旋转只会倒转一个轴。
        if first[0] >= last[2] and first[1] >= last[3]:
            reversed_tables.append((table["pred_html"], first, last))

    targets = set()
    for label, html, region in _layout_blocks(result):
        if label != "table" or not any(
            html == predicted and _contains_center(region, first) and _contains_center(region, last)
            for predicted, first, last in reversed_tables
        ):
            continue
        votes = [angle for angle, box, score, text in zip(angles, boxes, scores, texts)
                 if angle in (0, 1) and score >= 0.85 and _contains_center(region, box)
                 and int(box[2]) - int(box[0]) > int(box[3]) - int(box[1])
                 and any(char.isalpha() for char in text)]
        if len(votes) >= 3 and votes.count(0) * 5 >= len(votes) * 4:
            targets.add(region)
    return targets


class PaddleDocumentEngine:
    """PP-StructureV3 引擎：渲染页图 → 统一块结构。所有文档页的唯一后端。

    extra_options 用于 A/B 实验（如开印章识别/方向矫正），覆盖同名默认开关。
    device="cpu" | "gpu" | "gpu:0"；GPU 下省略 mkldnn/cpu_threads 这两个 CPU 专属开关。
    """
    def __init__(self, cpu_threads=4, extra_options: dict | None = None,
                 device: str = "cpu"):
        self.cpu_threads = cpu_threads
        self.device = device
        self.options = {**_STRUCTURE_OPTIONS, **(extra_options or {})}
        self._pipeline = None

    def warm(self):
        """启动时预加载模型（急加载），避免首个任务承担初始化耗时。"""
        self._ensure_pipeline()
        return self

    def _ensure_pipeline(self):
        if self._pipeline is None:
            from paddleocr import PPStructureV3
            options = dict(device=self.device)
            if self.device == "cpu":
                options.update(cpu_threads=self.cpu_threads,
                               enable_mkldnn=mkldnn_validated())
            self._pipeline = PPStructureV3(**options, **self.options)

    def _recheck_tables(self, image, targets):
        replacements = {}
        try:
            for result in self._pipeline.predict(image, use_table_orientation_classify=False):
                for label, html, box in _layout_blocks(result):
                    if label == "table" and box in targets:
                        parser = _Rows()
                        parser.feed(str(html))
                        if any(cell.strip() for row in parser.rows for cell in row):
                            replacements[box] = html
        except Exception:
            log.warning("表格方向复读失败，保留首次识别结果", exc_info=True)
            return {}
        return replacements

    def __call__(self, image, page_num):
        self._ensure_pipeline()
        height, width = image.shape[:2]
        blocks = []
        labels = {"table": BlockType.TABLE, "doc_title": BlockType.TITLE,
                  "paragraph_title": BlockType.TITLE, "title": BlockType.TITLE,
                  "image": BlockType.FIGURE, "figure": BlockType.FIGURE,
                  "header": BlockType.PAGE_HEADER, "footer": BlockType.PAGE_FOOTER,
                  # seal 块的 content 是章内环形文字——显式归为文本块，
                  # 带位置坐标进结果，知识库可检索到盖章单位
                  "seal": BlockType.TEXT,
                  "figure_table_chart_title": BlockType.FIGURE_CAPTION}
        results = list(self._pipeline.predict(image))
        targets = {box for result in results for box in _table_recheck_boxes(result)}
        replacements = self._recheck_tables(image, targets) if targets else {}
        for result in results:
            for label, content, box in _layout_blocks(result):
                kind = labels.get(label, BlockType.TEXT)
                if kind == BlockType.TABLE:
                    content = replacements.get(box, content)
                    parser = _Rows(); parser.feed(str(content))
                    shadows = _shadow_rows(parser.rows)
                    blocks.append(Block(type=kind, bbox=_bbox(box, width, height),
                                        html=str(content), row_texts=shadows, text="\n".join(shadows)))
                else:
                    blocks.append(Block(type=kind, text=str(content), bbox=_bbox(box, width, height)))
        return blocks

    def close(self):
        if self._pipeline is not None:
            self._pipeline.close()


def process_document(path: str | Path, *, scan_reader=None, dpi: int = 144,
                     on_page: Callable[[PageResult, int], None] | None = None) -> DocumentResult:
    """PDF 逐页渲染 → StructureV3，所有页产出同构 blocks（source_mode 恒为 ocr）。

    on_page(page_result, total) 在每页处理完后回调，携带该页真实识别产物
    （blocks/耗时），任务层用它上报逐页进度。scan_reader=None 时自建引擎
    （供工具/实验直调），worker 传入常驻引擎。
    """
    import pymupdf
    if dpi < 72:
        raise ValueError("dpi must be at least 72")
    path = Path(path)
    started = time.perf_counter()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    pages = []; warnings = []
    owned_reader = scan_reader is None
    reader = scan_reader or PaddleDocumentEngine()
    try:
        with pymupdf.open(path) as document:
            total = document.page_count
            for page in document:
                page_started = time.perf_counter()
                import numpy as np
                pix = page.get_pixmap(dpi=dpi, alpha=False)
                rgb = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
                blocks = reader(rgb[:, :, ::-1].copy(), page.number+1)
                if not blocks:
                    warnings.append(f"第{page.number+1}页未提取到文字或表格")
                page_result = PageResult(page_num=page.number+1, source_mode=SourceMode.OCR,
                                         duration_ms=round((time.perf_counter()-page_started)*1000),
                                         width_px=pix.width, height_px=pix.height, blocks=blocks)
                pages.append(page_result)
                if on_page is not None:
                    on_page(page_result, total)
    finally:
        if owned_reader: reader.close()
    return DocumentResult(file_sha256=digest, filename=path.name, pages=pages,
                          total_duration_ms=round((time.perf_counter()-started)*1000),
                          quality_warnings=warnings)
