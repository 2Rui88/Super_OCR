"""Office → PDF 转换 —— LibreOffice headless，产物按内容哈希复用。

约定：
- 上传件已按 `<sha256><ext>` 命名落盘，转换产物固定存 `<out_dir>/<sha256>.pdf`；
  同一内容重复提交直接复用，转换只做一次。
- soffice 用独立 UserInstallation 临时目录启动，多 worker 并行转换互不抢锁。
- 转换在临时目录完成后原子改名进产物路径，读到半成品的窗口不存在。
- 页数超上限 / 超时 / 无产物 → ConvertError（error_code 进任务终态，永久失败）。
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path


class ConvertError(Exception):
    """转换失败。error_code: CONVERT_FAILED（转换不出/超时）| DOC_TOO_LONG（页数超限）。"""

    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code


def is_office_document(path: str | Path) -> bool:
    return Path(path).suffix.lower() in {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"}


def _count_pages(pdf_path: Path) -> int:
    import pymupdf

    with pymupdf.open(pdf_path) as document:
        return document.page_count


def convert_office_to_pdf(src: str | Path, out_dir: str | Path, *,
                          soffice_bin: str = "soffice",
                          timeout_seconds: float = 120.0,
                          max_pages: int = 200) -> Path:
    """转换单个 Office 文件，返回产物 PDF 路径（已在 out_dir，名为 <src_stem>.pdf）。"""
    src = Path(src)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{src.stem}.pdf"
    if target.exists():  # 同内容之前已转换过（上传件按哈希命名，stem 即内容哈希）
        return target

    with tempfile.TemporaryDirectory(prefix="soffice-") as workdir:
        # 独立 profile：soffice 同 profile 并行会互抢锁，各自临时目录互不影响
        profile = Path(workdir) / "profile"
        profile.mkdir()
        try:
            result = subprocess.run(
                [soffice_bin, "--headless", "--norestore", "--nolockcheck",
                 f"-env:UserInstallation=file://{profile.as_posix()}",
                 "--convert-to", "pdf", "--outdir", workdir, str(src)],
                capture_output=True, timeout=timeout_seconds, check=False)
        except subprocess.TimeoutExpired:
            raise ConvertError("CONVERT_FAILED",
                               f"LibreOffice 转换超时（>{timeout_seconds:.0f}s）：{src.name}")
        produced = Path(workdir) / f"{src.stem}.pdf"
        if result.returncode != 0 or not produced.exists():
            detail = (result.stderr or result.stdout or b"").decode("utf-8", "replace").strip()
            raise ConvertError("CONVERT_FAILED",
                               f"LibreOffice 转换失败（{src.name}）：{detail[:500] or '无输出'}")
        pages = _count_pages(produced)
        if pages > max_pages:
            raise ConvertError(
                "DOC_TOO_LONG",
                f"转换后共 {pages} 页，超过 {max_pages} 页上限（{src.name}）")
        produced.replace(target)  # 同盘原子改名
    return target


def ensure_pdf(path: str | Path, converted_dir: str | Path, *,
               soffice_bin: str = "soffice",
               timeout_seconds: float = 120.0,
               max_pages: int = 200) -> tuple[Path, bool]:
    """文档任务入口的统一前置：PDF 原样返回；Office 转换后返回产物路径。

    返回 (pdf_path, converted)。converted=True 表示本次发生了实际转换
    （False = 本来就是 PDF，或命中既有转换产物）。
    """
    path = Path(path)
    if not is_office_document(path):
        return path, False
    before = (Path(converted_dir) / f"{path.stem}.pdf").exists()
    pdf = convert_office_to_pdf(path, converted_dir, soffice_bin=soffice_bin,
                                timeout_seconds=timeout_seconds, max_pages=max_pages)
    return pdf, not before
