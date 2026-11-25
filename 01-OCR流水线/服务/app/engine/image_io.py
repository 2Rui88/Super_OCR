"""Fully decode image uploads before using their pixels."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageFile, ImageOps


class InvalidImageError(ValueError):
    """The original image could not be decoded safely."""


INVALID_IMAGE_MESSAGE = "文件内容不是完整有效的图片，请重新上传"


@contextmanager
def open_decoded_image(source: BytesIO | Path) -> Iterator[Image.Image]:
    if ImageFile.LOAD_TRUNCATED_IMAGES:
        raise RuntimeError("Pillow must be configured for strict image decoding")
    try:
        with Image.open(source) as image:
            image.load()
            yield image
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError) as exc:
        raise InvalidImageError(INVALID_IMAGE_MESSAGE) from exc


def load_bgr(path: Path):
    import numpy as np

    with open_decoded_image(path) as image:
        ImageOps.exif_transpose(image, in_place=True)
        rgb = np.asarray(image.convert("RGB"))
    return rgb[:, :, ::-1].copy()
