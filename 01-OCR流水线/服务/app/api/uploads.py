"""上传安全：扩展名白名单 + magic bytes 验真 + 大小上限；磁盘路径一律服务端生成。

用户原始文件名只作为元数据入库（SQL 参数化，无注入面），绝不参与磁盘路径。
"""
from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path

from fastapi import HTTPException

from app.engine.image_io import INVALID_IMAGE_MESSAGE, open_decoded_image

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
PDF_EXTENSIONS = {".pdf"}
# Office 文档：上传后由 worker 统一转 PDF 再走文档链路
OFFICE_EXTENSIONS = {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"}
DOCUMENT_EXTENSIONS = PDF_EXTENSIONS | OFFICE_EXTENSIONS

# OLE2 复合文档头（.doc/.xls/.ppt）；.docx/.xlsx/.pptx 是 ZIP 容器（PK 头）
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _reject(status: int, code: str, message: str):
    raise HTTPException(status_code=status, detail={"error_code": code, "message": message})


async def read_bounded(upload, max_mb: int) -> bytes:
    """限长读取：最多读 上限+1 字节，超限直接 413。

    客户端声明的大小不可信，先 await upload.read() 全量读再判长度会让恶意
    大文件先占满内存。read(size) 只取前 size 字节，内存占用被钉死在上限。
    """
    limit = max_mb * 1024 * 1024
    content = await upload.read(limit + 1)
    if len(content) > limit:
        _reject(413, "FILE_TOO_LARGE", f"文件超过 {max_mb}MB 上限")
    return content


def save_upload(content: bytes, original_filename: str, *, allowed: set[str],
                upload_dir: Path, max_mb: int) -> tuple[str, Path]:
    """校验并落盘，返回 (sha256, 存储路径)。同名同内容天然去重。"""
    ext = Path(original_filename or "").suffix.lower()
    if ext not in allowed:
        _reject(415, "UNSUPPORTED_FORMAT",
                f"不支持的文件类型，只接受 {' / '.join(sorted(allowed))}")
    if len(content) > max_mb * 1024 * 1024:
        _reject(413, "FILE_TOO_LARGE", f"文件超过 {max_mb}MB 上限")
    if ext == ".pdf":
        if not content.startswith(b"%PDF-"):
            _reject(415, "UNSUPPORTED_FORMAT", "文件内容不是有效的 PDF")
    elif ext in OFFICE_EXTENSIONS:
        if ext in {".docx", ".xlsx", ".pptx"}:
            valid = content.startswith(b"PK")  # OOXML 是 ZIP 容器
            problem = "文件内容不是有效的 Office 文档（OOXML/ZIP）"
        else:
            valid = content.startswith(_OLE2_MAGIC)
            problem = "文件内容不是有效的 Office 文档（OLE2）"
        if not valid:
            _reject(415, "UNSUPPORTED_FORMAT", problem)
    else:
        from PIL import Image

        try:
            with Image.open(BytesIO(content)) as im:
                im.verify()
            with open_decoded_image(BytesIO(content)):
                pass
        except (OSError, SyntaxError, ValueError, Image.DecompressionBombError):
            _reject(415, "UNSUPPORTED_FORMAT", INVALID_IMAGE_MESSAGE)
    digest = hashlib.sha256(content).hexdigest()
    path = upload_dir / f"{digest}{ext}"
    if not path.exists():
        upload_dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return digest, path
