"""图片路由 —— 一次 PP-OCRv6 识别，先判证件，未命中转文档图片识别。

PP-OCRv6 medium 同时承担两件事：产出全部文字行（含置信度和坐标），
以及通过 YAML 锚点规则判断这是不是支持的 4 类证件。命中证件时
复用同一次识别结果做字段抽取（不二次 OCR）；未命中时转 PP-StructureV3
按普通文档图片识别。返回统一信封 {kind, reason, result}。
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path

from app.certs import classify
from app.contracts import DocumentResult, PageResult, SourceMode
from app.core.card_pipeline import build_card_result

FALLBACK_REASON = "不是证件，已按文档识别"


def _load_bgr(path: Path):
    import numpy as np
    from PIL import Image

    with Image.open(path) as im:
        rgb = np.asarray(im.convert("RGB"))
    return rgb[:, :, ::-1].copy()  # PP-StructureV3 吃 BGR


def process_image(
    path: str | Path,
    *,
    card_engine,
    document_reader,
    registry,
    sha256: str = "",
    hint_type: str | None = None,
    card_det_side: int | None = None,
    card_rectified_out: Path | None = None,
) -> dict:
    """图片 → 统一信封（model_dump(mode="json")，可直接入库/回传）。

    document_reader(image_bgr, page_num) 只在需要兜底时才会被调用，
    worker 借此实现 PP-StructureV3 的懒加载。
    card_det_side 运行时覆盖检测长边（低置信重试 960→1280 用）。
    card_rectified_out 非空时把摆正图写到该路径（bbox 坐标系即该图）。
    """
    path = Path(path)
    digest = sha256 or hashlib.sha256(path.read_bytes()).hexdigest()
    t0 = time.perf_counter()

    kwargs = {}
    if card_det_side is not None:
        kwargs["det_side"] = card_det_side
    if card_rectified_out is not None:
        kwargs["rectified_out"] = card_rectified_out
    lines = card_engine.recognize_lines(path, **kwargs)
    full_text = "\n".join(line.text for line in lines)

    if hint_type and hint_type in registry:
        doc_type, conf = hint_type, 1.0
    else:
        doc_type, conf = classify(full_text, registry)

    if doc_type != "unknown":
        result = build_card_result(path, lines, registry, sha256=digest,
                                   doc_type=doc_type, conf=conf, started=t0)
        return {"kind": "card", "reason": "", "result": result.model_dump(mode="json")}

    from PIL import Image

    with Image.open(path) as im:
        width, height = im.size
    blocks = document_reader(_load_bgr(path), 1)
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    page = PageResult(page_num=1, source_mode=SourceMode.OCR, duration_ms=elapsed_ms,
                      width_px=width, height_px=height, blocks=blocks)
    result = DocumentResult(file_sha256=digest, filename=path.name, pages=[page],
                            total_duration_ms=elapsed_ms)
    return {"kind": "document", "reason": FALLBACK_REASON,
            "result": result.model_dump(mode="json")}
