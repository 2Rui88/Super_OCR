"""引擎抽象层 —— 业务代码只面对这里，不 import 任何具体引擎。

换引擎（PaddleOCR ↔ RapidOCR）= 只换 adapter，业务零改动。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class OcrLine:
    """一行文字：证件线的原子输入。bbox 归一化 0~1（相对原图）。"""

    text: str
    bbox: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0])
    confidence: float = 1.0

    @property
    def cx(self) -> float:
        return (self.bbox[0] + self.bbox[2]) / 2

    @property
    def cy(self) -> float:
        return (self.bbox[1] + self.bbox[3]) / 2


class BaseCardEngine(ABC):
    """证件线引擎：图片 → 行级 OCR 结果。

    recognize_lines 的 det_side 为可选扩展（运行时覆盖检测长边，低置信重试用）；
    不支持的实现可以忽略它——调用方仅在显式传入时才带该参数。
    """

    @abstractmethod
    def recognize_lines(self, image_path: str | Path,
                        det_side: int | None = None) -> list[OcrLine]:
        ...
