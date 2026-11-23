"""GET /api/v1/files/* —— 上传原件与摆正图只读回看；PDF 按页渲染页图。

安全约束：文件名必须是 `<64位hex>.<白名单扩展>` 或摆正图 `<64位hex>.rect.jpg`
的字面量（上传/摆正落盘的命名规则），不接收任何路径形态，杜绝目录穿越；
仅读取 upload_dir 下的既有文件。
"""
from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse

router = APIRouter(tags=["files"])

_NAME_RE = re.compile(
    r"^(?P<sha>[0-9a-f]{64})(?:\.rect)?\.(?P<ext>jpe?g|png|bmp|webp|pdf|docx?|xlsx?|pptx?)$",
    re.IGNORECASE)
_SHA_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)


@router.get("/files/{name}")
def get_uploaded_file(name: str, request: Request):
    if not _NAME_RE.match(name):
        raise HTTPException(status_code=400, detail={
            "error_code": "BAD_FILE_NAME",
            "message": "文件名必须是 <sha256>.<扩展名> 形式"})
    settings = request.app.state.settings
    path = (settings.upload_dir / name).resolve()
    if settings.upload_dir.resolve() not in path.parents:
        raise HTTPException(status_code=400, detail={   # 理论上正则已挡住，双保险
            "error_code": "BAD_FILE_NAME", "message": "非法路径"})
    if not path.is_file():
        raise HTTPException(status_code=404, detail={
            "error_code": "FILE_NOT_FOUND", "message": "原件不存在或已清理"})
    return FileResponse(path)


def _locate_pdf(sha: str, request: Request):
    """按内容哈希定位待渲染 PDF：原生 PDF 直取，Office 件取转换产物。"""
    settings = request.app.state.settings
    for candidate in (settings.upload_dir / f"{sha}.pdf",
                      settings.upload_dir / "converted" / f"{sha}.pdf"):
        if candidate.is_file():
            return candidate
    raise HTTPException(status_code=404, detail={
        "error_code": "FILE_NOT_FOUND", "message": "原件不存在或已清理"})


@router.get("/files/{sha}/page/{page_num}")
def get_page_image(sha: str, page_num: int, request: Request):
    """渲染 PDF 某一页为 PNG（dpi=144，与识别时一致，bbox 比例天然对齐）。"""
    if not _SHA_RE.match(sha):
        raise HTTPException(status_code=400, detail={
            "error_code": "BAD_FILE_NAME", "message": "sha 必须是 64 位十六进制"})
    if page_num < 1:
        raise HTTPException(status_code=400, detail={
            "error_code": "BAD_PAGE", "message": "页码从 1 开始"})
    import pymupdf

    pdf = _locate_pdf(sha, request)
    with pymupdf.open(pdf) as document:
        if page_num > document.page_count:
            raise HTTPException(status_code=404, detail={
                "error_code": "PAGE_NOT_FOUND",
                "message": f"超出总页数（{document.page_count}）"})
        pix = document[page_num - 1].get_pixmap(dpi=144, alpha=False)
        return Response(pix.tobytes("png"), media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})
