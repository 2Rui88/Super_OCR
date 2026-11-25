"""Configurable local OCR with explicit models and a validated CPU backend."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import logging
import math
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from app.engine.base import BaseCardEngine, OcrLine
from app.engine.image_io import load_bgr

if TYPE_CHECKING:
    from numpy import ndarray

log = logging.getLogger(__name__)


def paddle_package_name() -> str:
    """当前安装的是 CPU 还是 GPU 发行包（GPU 包名是 paddlepaddle-gpu）。"""
    for name in ("paddlepaddle", "paddlepaddle-gpu"):
        try:
            importlib.metadata.version(name)
            return name
        except importlib.metadata.PackageNotFoundError:
            continue
    return "paddlepaddle"  # 未安装时留给上层报错


def runtime_versions() -> dict[str, str]:
    """运行时版本签名。GPU 包在版本前带包名前缀，确保 CPU/GPU 缓存键天然分离；
    CPU 安装的取值与历史版本完全一致（不失效既有缓存）。"""
    versions = {}
    for name in ("paddlepaddle", "paddleocr", "paddlex"):
        try:
            version = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            version = "not-installed"
        if name == "paddlepaddle" and version != "not-installed":
            package = paddle_package_name()
            if package != "paddlepaddle":
                version = f"{package}:{version}"
        versions[name] = version
    return versions


def mkldnn_validated(device: str = "cpu") -> bool:
    """oneDNN 只对实测过的 CPU 版 Paddle 开启（3.3.1 有 PIR 属性转换 bug，见 README）；
    GPU 设备下 oneDNN 无意义，直接关闭。"""
    if device != "cpu":
        return False
    try:
        return importlib.metadata.version("paddlepaddle") == "3.2.2"
    except importlib.metadata.PackageNotFoundError:
        return False


@dataclass(frozen=True)
class PaddleCardConfig:
    detection_max_side: int | None = 960
    cpu_threads: int = 4
    backend: str = "auto"
    device: str = "cpu"  # "cpu" 或 "gpu" / "gpu:0"（进入 cache_signature，CPU/GPU 缓存分离）
    detection_model_name: str = "PP-OCRv6_medium_det"
    recognition_model_name: str = "PP-OCRv6_medium_rec"
    use_doc_orientation_classify: bool = True
    # UVDoc 实验(2026-11-19,GPU 43 张):关闭后 bbox 回归原图坐标系(垂直就位
    # 5-7/20 → 13-18/20),透视/水印/换行各 +1~3 字段;但 rotate 倾斜件 24/25 →
    # 10/25 灾难回退,总分 96.70% → 93.41%。故默认保持开启;框位问题走方案 B
    # (低置信时以 UVDoc 关的备用引擎重跑取框)。完整数据见 tests/_out/。
    use_doc_unwarping: bool = True
    use_textline_orientation: bool = True

    def __post_init__(self):
        if self.cpu_threads < 1:
            raise ValueError("cpu_threads must be positive")
        if self.detection_max_side is not None and self.detection_max_side < 32:
            raise ValueError("detection_max_side must be at least 32")
        if self.backend not in {"auto", "paddle", "mkldnn"}:
            raise ValueError("backend must be auto, paddle or mkldnn")
        if self.device != "cpu" and not self.device.startswith("gpu"):
            raise ValueError("device must be 'cpu', 'gpu' or 'gpu:<idx>'")
        if not self.detection_model_name or not self.recognition_model_name:
            raise ValueError("Explicit detection and recognition model names are required")


class PaddleCardEngine(BaseCardEngine):
    def __init__(self, lang: str = "ch", *, config: PaddleCardConfig | None = None):
        from paddleocr import PaddleOCR

        if lang != "ch":
            raise ValueError("Configured certificate models currently support lang='ch'")
        self.config = config or PaddleCardConfig()
        self.runtime_versions = runtime_versions()
        on_gpu = self.config.device != "cpu"
        validated = mkldnn_validated(self.config.device)
        self.enable_mkldnn = self.config.backend == "mkldnn" or (
            self.config.backend == "auto" and validated
        )
        if self.config.backend == "auto" and not validated and not on_gpu:
            log.warning("CPU acceleration disabled for unvalidated Paddle %s; install "
                        "the pinned requirements for the measured fast runtime",
                        self.runtime_versions["paddlepaddle"])
        options = dict(
            text_detection_model_name=self.config.detection_model_name,
            text_recognition_model_name=self.config.recognition_model_name,
            use_doc_orientation_classify=self.config.use_doc_orientation_classify,
            use_doc_unwarping=self.config.use_doc_unwarping,
            use_textline_orientation=self.config.use_textline_orientation,
            device=self.config.device,
        )
        if not on_gpu:
            # mkldnn / cpu_threads 是 CPU 专属开关，GPU 下省略
            options.update(enable_mkldnn=self.enable_mkldnn,
                           cpu_threads=self.config.cpu_threads)
        if self.config.detection_max_side is not None:
            options.update(text_det_limit_side_len=self.config.detection_max_side,
                           text_det_limit_type="max")
        self._ocr = PaddleOCR(**options)
        self._lock = threading.Lock()

    @property
    def cache_signature(self) -> str:
        payload = dict(schema=1, config=asdict(self.config),
                       runtime=self.runtime_versions, mkldnn=self.enable_mkldnn)
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def recognize_lines(self, image_path: str | Path,
                        det_side: int | None = None,
                        rectified_out: Path | None = None) -> list[OcrLine]:
        lines, _ = self.recognize_frame(image_path, det_side, rectified_out)
        return lines

    def recognize_frame(self, image_path: str | Path,
                        det_side: int | None = None,
                        rectified_out: Path | None = None) -> tuple[list[OcrLine], ndarray | None]:
        """返回 OCR 行和同一次推理实际使用的 BGR 图，坐标均相对于该图。"""
        source = load_bgr(Path(image_path))
        overrides = {}
        if det_side is not None and det_side >= 32:
            overrides.update(text_det_limit_side_len=det_side,
                             text_det_limit_type="max")
        with self._lock:
            results = self._predict_with_word_boxes(source, **overrides)
        if not results:
            return [], None
        result = results[0]
        preprocessed = result.get("doc_preprocessor_res", {})
        frame = preprocessed.get("output_img")
        if frame is not None:
            height, width = frame.shape[:2]
            if rectified_out is not None:
                self._save_rectified(frame, rectified_out)
        else:
            height, width = source.shape[:2]
        return self._result_lines(result, width, height), frame

    def _predict_with_word_boxes(self, image, **options):
        try:
            return self._ocr.predict(image, return_word_box=True, **options)
        except (TypeError, ValueError, IndexError, ZeroDivisionError):
            log.warning("词坐标不可用，回退到行级识别", exc_info=True)
            return self._ocr.predict(image, return_word_box=False, **options)

    @staticmethod
    def _polygon_bbox(poly, width: int, height: int, x: int, y: int) -> list[float]:
        xs = [float(point[0]) + x for point in poly]
        ys = [float(point[1]) + y for point in poly]
        return [max(0.0, min(xs) / width), max(0.0, min(ys) / height),
                min(1.0, max(xs) / width), min(1.0, max(ys) / height)]

    @classmethod
    def _word_boxes(cls, text, tokens, regions, width: int, height: int,
                    x: int, y: int) -> list[tuple[int, int, list[float]]]:
        words = []
        offset = 0
        for token, region in zip(tokens, regions):
            start = text.find(token, offset)
            if start < 0 or not token:
                continue
            offset = start + len(token)
            if not token.isspace():
                bbox = cls._polygon_bbox(region, width, height, x, y)
                if bbox[0] < bbox[2] and bbox[1] < bbox[3]:
                    words.append((start, offset, bbox))
        return words

    @classmethod
    def _result_lines(cls, result, width: int, height: int, x: int = 0, y: int = 0) -> list[OcrLine]:
        lines = []
        tokens = result.get("text_word", [])
        regions = result.get("text_word_region", [])
        recognized = zip(result["rec_texts"], result["rec_scores"], result["rec_polys"])
        for index, (text, score, poly) in enumerate(recognized):
            text = str(text)
            bbox = cls._polygon_bbox(poly, width, height, x, y)
            words = (cls._word_boxes(text, tokens[index], regions[index], width, height, x, y)
                     if index < min(len(tokens), len(regions)) else [])
            lines.append(OcrLine(text, bbox, float(score), words))
        return lines

    def recognize_region(self, frame: ndarray, bbox: list[float], multi_line: bool) -> list[OcrLine]:
        """只识别字段裁图。单行跳过检测，多行在裁图内检测，结果坐标映射回摆正图。"""
        height, width = frame.shape[:2]
        x0, y0 = math.floor(bbox[0] * width), math.floor(bbox[1] * height)
        x1, y1 = math.ceil(bbox[2] * width), math.ceil(bbox[3] * height)
        if x1 - x0 < 8 or y1 - y0 < 8:
            return []
        crop = frame[y0:y1, x0:x1].copy()
        with self._lock:
            if multi_line:
                results = self._predict_with_word_boxes(crop, use_doc_orientation_classify=False,
                                                       use_doc_unwarping=False)
                return [line for result in results
                        for line in self._result_lines(result, width, height, x0, y0)]
            recognizer = self._ocr.paddlex_pipeline.text_rec_model
            result = next(iter(recognizer([crop], return_word_box=True)), None)
        if result is None:
            return []
        crop_bbox = [x0 / width, y0 / height, x1 / width, y1 / height]
        text = result["rec_text"]
        words = []
        if isinstance(text, (list, tuple)):
            import numpy as np
            from paddlex.inference.pipelines.components.common.cal_ocr_word_box import cal_ocr_word_box

            text, word_info = text
            crop_height, crop_width = crop.shape[:2]
            region = np.array([[0, 0], [crop_width, 0], [crop_width, crop_height], [0, crop_height]])
            try:
                tokens, regions = cal_ocr_word_box(text, region, word_info)
                words = self._word_boxes(text, tokens, regions, width, height, x0, y0)
            except (TypeError, ValueError, IndexError, ZeroDivisionError):
                log.debug("局部词坐标不可用，沿用裁剪区坐标", exc_info=True)
        return [OcrLine(str(text), crop_bbox, float(result["rec_score"]), words)]

    @staticmethod
    def _save_rectified(frame: ndarray, out_path: Path) -> bool:
        """把同一张 BGR 摆正图另存为 JPEG。"""
        from PIL import Image

        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            Image.fromarray(frame[..., ::-1]).save(out_path, quality=90)
            return True
        except Exception:
            return False  # 摆正图属附加产物，落盘失败不影响识别主流程

    def close(self):
        with self._lock:
            self._ocr.close()
