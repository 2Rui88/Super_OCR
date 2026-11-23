"""Configurable local OCR with explicit models and a validated CPU backend."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import logging
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from app.engine.base import BaseCardEngine, OcrLine

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
        """det_side 运行时覆盖检测长边（如低置信重试时 960→1280），
        依赖 PaddleOCR predict() 支持同名参数——是否生效需在目标环境证伪验证。

        rectified_out 非空时，把 doc_preprocessor 的摆正结果图（UVDoc 矫正后、
        det/rec 实际运行的画布，与原图同尺寸）另存为 JPEG——bbox 属于该图坐标系，
        前端"摆正视图"用它画框即可精确对齐。"""
        from PIL import Image

        path = Path(image_path)
        with Image.open(path) as im:
            width, height = im.size
        overrides = {}
        if det_side is not None and det_side >= 32:
            overrides.update(text_det_limit_side_len=det_side,
                             text_det_limit_type="max")
        with self._lock:
            results = self._ocr.predict(str(path), **overrides)
        rectified_saved = False
        lines = []
        for res in results:
            if rectified_out is not None and not rectified_saved:
                rectified_saved = self._save_rectified(res, rectified_out)
            for text, score, poly in zip(res["rec_texts"], res["rec_scores"], res["rec_polys"]):
                xs = [float(p[0]) for p in poly]
                ys = [float(p[1]) for p in poly]
                bbox = [max(0.0, min(xs) / width), max(0.0, min(ys) / height),
                        min(1.0, max(xs) / width), min(1.0, max(ys) / height)]
                lines.append(OcrLine(str(text), bbox, float(score)))
        return lines

    @staticmethod
    def _save_rectified(result, out_path: Path) -> bool:
        """把 predict 结果里的摆正图（BGR ndarray）落盘；无该字段时静默跳过。"""
        import numpy as np
        from PIL import Image

        try:
            dp = result["doc_preprocessor_res"]
            img = dp["output_img"] if "output_img" in dp else None
            if img is None:
                return False
            arr = np.asarray(img)
            if arr.ndim != 3:
                return False
            out_path.parent.mkdir(parents=True, exist_ok=True)
            Image.fromarray(arr[..., ::-1]).save(out_path, quality=90)
            return True
        except Exception:
            return False  # 摆正图属附加产物，落盘失败不影响识别主流程

    def close(self):
        with self._lock:
            self._ocr.close()
