"""摄入管线 A2–A5：转格式 → 识别存档 → 过滤打档案（章节面包屑）→ 分装。

A6 裁图/VL、A7 向量化上架在阶段三扩展（vector.py / figures.py），
本模块只产出 chunks 行（embedding 留空）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from app import figures, llm, queue, vector
from app.config import settings
from app.p1client import P1Error, recognize_pdf

log = logging.getLogger("kb.ingest")

# 项目一块类型 → P2 处置
_DROP_TYPES = {"page_header", "page_footer"}
_TITLE_TYPES = {"title"}

# 制度文件标题兜底：文字层直读没有 TITLE 块（P1 只在 OCR 路径产出），
# 用章节号模式从正文识别——对直读/OCR 两条路径都生效。
_HEADING_RE = re.compile(
    r"^\s*(第[一二三四五六七八九十百千\d]+[章节篇][\s　]*[^\s，。；]{0,20}"
    r"|附录[\s　]*[A-Za-z\d一二三四五六七八九十]+[^\s，。；]{0,20}"
    r"|[一二三四五六七八九十]+[、.．][^\s，。；]{2,20})")


def _detect_heading(text: str) -> str | None:
    m = _HEADING_RE.match(text or "")
    return m.group(1).strip() if m else None


# ---------------------------------------------------------------- A2 转格式

def _find_soffice() -> str | None:
    """PATH 优先；winget 安装后当前进程 PATH 可能未刷新，补探标准安装路径。"""
    if settings.soffice_path:
        return settings.soffice_path
    found = shutil.which("soffice")
    if found:
        return found
    for base in (r"C:\Program Files\LibreOffice\program",
                 r"C:\Program Files (x86)\LibreOffice\program"):
        candidate = Path(base) / "soffice.exe"
        if candidate.exists():
            return str(candidate)
    return None


def convert_to_pdf(src: Path, job: queue.ClaimedDoc) -> Path:
    """统一收敛到 PDF（此后只有一条解析路径）。pdf 直通；doc/docx 走 soffice，
    本机无 soffice 时退 Word COM。转换产物按内容寻址，重复导入零转换。"""
    if src.suffix.lower() == ".pdf":
        return src
    out_dir = settings.data_dir / "converted"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_pdf = out_dir / f"{job.sha256[:16]}.pdf"
    if out_pdf.exists():
        return out_pdf
    # 转换线程可能在协程取消后继续运行。临时文件独立，持有租约才能发布缓存。
    with tempfile.TemporaryDirectory(dir=out_dir) as directory:
        tmp_dir = Path(directory)
        tmp_pdf = tmp_dir / f"{src.stem}.pdf"
        soffice = _find_soffice()
        if soffice:
            profile = (tmp_dir.resolve() / "profile").as_uri()
            cmd = [soffice, f"-env:UserInstallation={profile}", "--headless", "--norestore",
                   "--convert-to", "pdf", "--outdir", str(tmp_dir), str(src)]
            proc = _run_cmd(cmd)
            if proc.returncode != 0 or not tmp_pdf.exists():
                raise RuntimeError(f"LibreOffice 转换失败（rc={proc.returncode}）")
        else:
            _word_com_convert(src, tmp_pdf)
        with queue.lease_write(job.id, job.lease_token):
            if not out_pdf.exists():
                tmp_pdf.replace(out_pdf)
    return out_pdf


def _run_cmd(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=180)


def _word_com_convert(src: Path, dst: Path) -> None:
    """兜底：Word/WPS COM（本机未装 LibreOffice 时）。"""
    import pythoncom  # type: ignore
    import win32com.client  # type: ignore

    pythoncom.CoInitialize()
    word = None
    doc = None
    try:
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        doc = word.Documents.Open(str(src.resolve()), ReadOnly=True)
        doc.SaveAs2(str(dst.resolve()), FileFormat=17)  # 17 = wdFormatPDF
    finally:
        if doc is not None:
            doc.Close(False)
        if word is not None:
            word.Quit()
        pythoncom.CoUninitialize()


# ---------------------------------------------------------------- A4+A5 打档案与分装

def pack_chunks(doc_id: int, lease_token: str, filename: str,
                p1_result: dict) -> tuple[int, int, int]:
    """过滤页眉页脚、维护章节面包屑、按类型分装，写入 chunks 行。

    返回 (页数, 表格数, 流程图数)。embeddings 阶段三回填。
    """
    rows: list[dict] = []
    heading = ""            # 章节面包屑：最近一次 TITLE 的文字
    pending_text: list[str] = []
    pending_page = 0
    pending_bbox: list[float] = [0, 0, 0, 0]
    pending_mode = ""
    pending_conf: list[float] = []
    pending_heading = ""

    def flush_text() -> None:
        nonlocal pending_text
        if not pending_text:
            return
        text = "".join(pending_text)
        if text.strip():
            rows.append(_chunk(
                doc_id, pending_page, pending_bbox, "text", None,
                pending_heading, f"{filename}\n{text}", None, None, None,
                pending_mode, _min_conf(pending_conf)))
        pending_text = []

    for page in p1_result.get("pages", []):
        page_mode = page.get("source_mode", "")
        for block in page.get("blocks", []):
            btype = block.get("type", "text")
            if btype in _DROP_TYPES:
                continue
            bbox = block.get("bbox") or [0, 0, 0, 0]
            conf = block.get("confidence")
            confs = [conf] if conf is not None else []
            if btype in _TITLE_TYPES and block.get("text", "").strip():
                flush_text()
                heading = block["text"].strip()[:60]
            elif btype == "text":
                detected = _detect_heading(block.get("text", ""))
                if detected and len(block.get("text", "")) < 60:
                    flush_text()
                    heading = detected[:60]
            if btype == "table":
                flush_text()
                _pack_table(rows, doc_id, filename, page, block, heading)
            elif btype in ("figure",):
                flush_text()
                # linear_text 缺失时用块 text 兜底（P1 部分路径不产线性文本）
                fig_linear = block.get("linear_text") or block.get("text") or ""
                rows.append(_chunk(
                    doc_id, page["page_num"], bbox, "figure", None, heading,
                    f"{filename}\n{block.get('caption') or ''}\n{fig_linear}",
                    None, None, fig_linear, page_mode, _min_conf(confs)))
            elif btype in ("figure_caption",):
                caption = block.get("text", "").strip()
                if caption:
                    # 图注并入同页最近的 figure 块（不单独成块）
                    for row in reversed(rows):
                        if row["type"] == "figure" and row["page_num"] == page["page_num"]:
                            row["seek_text"] = row["seek_text"] + "\n" + caption
                            break
                    else:
                        heading = caption[:60]  # 图注先于图出现时充当面包屑
            else:  # text / title 正文流
                text = block.get("text", "")
                if not text.strip():
                    continue
                if (pending_page != page["page_num"] or
                        sum(len(t) for t in pending_text) + len(text)
                        > settings.text_merge_max_chars):
                    flush_text()
                if not pending_text:
                    pending_page = page["page_num"]
                    pending_bbox = list(bbox)
                    pending_mode = page_mode
                    pending_heading = heading
                else:
                    pending_bbox = _union(pending_bbox, bbox)
                if btype in _TITLE_TYPES:
                    text = f"【{heading}】{text}" if text.strip() != heading else text
                pending_text.append(text)
                pending_conf.extend(confs)
    flush_text()

    with queue.lease_write(doc_id, lease_token) as db:
        db.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
        db.executemany(
            "INSERT INTO chunks (doc_id, page_num, bbox, type, row_index, heading_path,"
            " seek_text, html, crop_path, linear_text, source_mode, ocr_confidence, created_at)"
            " VALUES (:doc_id, :page_num, :bbox, :type, :row_index, :heading_path,"
            " :seek_text, :html, :crop_path, :linear_text, :source_mode, :ocr_confidence, :created_at)",
            rows)

    pages = len(p1_result.get("pages", []))
    tables = len({(r["page_num"], r["bbox"]) for r in rows
                  if r["type"] in ("table_row", "table_full")})
    figures = sum(1 for r in rows if r["type"] == "figure")
    return pages, tables, figures


def _pack_table(rows: list[dict], doc_id: int, filename: str, page: dict,
                block: dict, heading: str) -> None:
    """表格双粒度：整表一块（展示）+ 每行一块（行影子文本检索）。"""
    html = block.get("html")
    row_texts: list[str] = block.get("row_texts") or []
    bbox = block.get("bbox") or [0, 0, 0, 0]
    page_num = page["page_num"]
    mode = page.get("source_mode", "")
    conf = block.get("confidence")
    whole = "\n".join(row_texts)
    rows.append(_chunk(
        doc_id, page_num, bbox, "table_full", None, heading,
        f"{filename}\n{whole}", html, None, None, mode, conf))
    for idx, row_text in enumerate(row_texts):
        if not row_text.strip():
            continue
        rows.append(_chunk(
            doc_id, page_num, bbox, "table_row", idx, heading,
            f"{filename}\n{row_text}", html, None, None, mode, conf))


def _chunk(doc_id, page_num, bbox, ctype, row_index, heading, seek_text,
           html, crop_path, linear_text, source_mode, conf) -> dict:
    return {
        "doc_id": doc_id, "page_num": page_num,
        "bbox": json.dumps([round(float(v), 4) for v in bbox]),
        "type": ctype, "row_index": row_index, "heading_path": heading or "",
        "seek_text": seek_text.strip(), "html": html, "crop_path": crop_path,
        "linear_text": linear_text, "source_mode": source_mode,
        "ocr_confidence": conf, "created_at": int(time.time()),
    }


def _min_conf(confs: list[float]) -> float | None:
    return min(confs) if confs else None


def _union(a: list[float], b: list[float]) -> list[float]:
    return [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]


# ---------------------------------------------------------------- 流程图补救

_RESCUE_IMAGE_RATIO = 0.30  # 直读页图像面积占比超过该值且无表格/图形块 → 补救


def _page_to_image_pdf(pdf_path: str, page_num: int, sha256: str) -> Path:
    """把指定页渲染成单页图片 PDF（144dpi 与 P1 同基准）。图片 PDF 无文字层，
    P1 的页面分流自然走整页 PP-StructureV3 → 产出 FIGURE 块。"""
    import pymupdf

    out_dir = settings.data_dir / "rescue"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_pdf = out_dir / f"{sha256[:16]}_p{page_num}.pdf"
    if out_pdf.exists():
        return out_pdf
    tmp = out_pdf.with_suffix(".tmp.pdf")
    with pymupdf.open(pdf_path) as doc:
        pix = doc[page_num - 1].get_pixmap(dpi=144, alpha=False)
        img = pix.tobytes("png")
    with pymupdf.open() as new:  # 单页图片 PDF
        page = new.new_page(width=pix.width / 2, height=pix.height / 2)  # 72dpi 基准
        page.insert_image(page.rect, stream=img)
        new.save(str(tmp))
    tmp.replace(out_pdf)
    return out_pdf


async def rescue_figures(job, pdf_path: str | None, p1_result: dict, set_phase) -> int:
    """转换类/电子 PDF 的盲点补救：文字层完好的大图页走直读路径不检测图形。

    判据（幂等，重放安全）：source_mode=text_layer 且图像占比≥30% 且该页
    无表格/图形块。整页块替换为 OCR 结果，source_mode 更新为 ocr（归因更真实）。
    """
    if not pdf_path or not Path(pdf_path).exists():
        return 0

    def _candidates() -> list[int]:
        import pymupdf
        pages = {p["page_num"]: p for p in p1_result.get("pages", [])}
        out = []
        with pymupdf.open(pdf_path) as doc:
            for page in doc:
                res = pages.get(page.number + 1)
                if not res or res.get("source_mode") != "text_layer":
                    continue
                if any(b.get("type") in ("table", "figure") for b in res.get("blocks", [])):
                    continue
                area = sum(pymupdf.Rect(i["bbox"]).get_area()
                           for i in page.get_image_info())
                if area >= page.rect.get_area() * _RESCUE_IMAGE_RATIO:
                    out.append(page.number + 1)
        return out

    targets = await asyncio.to_thread(_candidates)
    for n in targets:
        set_phase(f"流程图补救 第{n}页")
        single = await asyncio.to_thread(_page_to_image_pdf, pdf_path, n, job.sha256)
        ocr_result = await recognize_pdf(
            str(single), alive=lambda: queue.holds_lease(job.id, job.lease_token))
        ocr_page = ocr_result["pages"][0]
        for i, p in enumerate(p1_result["pages"]):
            if p["page_num"] == n:
                p1_result["pages"][i] = {
                    "page_num": n, "source_mode": "ocr",
                    "duration_ms": ocr_page.get("duration_ms", 0),
                    "width_px": ocr_page.get("width_px", 0),
                    "height_px": ocr_page.get("height_px", 0),
                    "blocks": ocr_page.get("blocks", []),
                }
                break
    return len(targets)


# ---------------------------------------------------------------- 编排入口

async def run_ingest(job: queue.ClaimedDoc) -> tuple[bool, str]:
    """从已保存的 OCR 提交或结果继续，完成切块与索引。"""

    def set_phase(text: str) -> None:
        queue.set_phase(job.id, job.lease_token, text)

    try:
        if job.p1_result_json:
            p1_result = json.loads(job.p1_result_json)
            pdf_path = job.pdf_path
        else:
            src = Path(job.pdf_path) if job.pdf_path else _stored_upload(job.sha256, job.filename)
            set_phase("转格式" if src.suffix.lower() != ".pdf" else "排队识别")
            pdf_path = str(await asyncio.to_thread(convert_to_pdf, src, job))

            def save_submission(request_key: str | None, task_id: int | None) -> None:
                queue.save_submission(job.id, job.lease_token, pdf_path, request_key, task_id)

            set_phase("识别中")
            p1_result = await recognize_pdf(
                pdf_path, task_id=job.p1_task_id, request_key=job.p1_request_key,
                save_submission=save_submission, on_phase=set_phase,
                alive=lambda: queue.holds_lease(job.id, job.lease_token))
            queue.save_ocr_result(job.id, job.lease_token, json.dumps(p1_result, ensure_ascii=False))
        rescued = await rescue_figures(job, pdf_path, p1_result, set_phase)
        if rescued:
            queue.save_ocr_result(job.id, job.lease_token, json.dumps(p1_result, ensure_ascii=False))
            log.info("流程图补救 %d 页：doc=%s", rescued, job.id)
        set_phase("分块入库")
        # 重放前摘除旧块的向量，再替换 SQLite 块。
        await vector.remove_doc(job.id, lease_token=job.lease_token)
        pages, tables, figure_count = await asyncio.to_thread(
            pack_chunks, job.id, job.lease_token, job.filename, p1_result)
        set_phase("图块加工")
        await figures.process_doc_figures(job.id, job.lease_token, pdf_path)
        set_phase("向量化上架")
        await embed_doc_chunks(job.id, job.lease_token, job.filename)
        await vector.add_chunks(job.id, job.lease_token)
        status = queue.finish(job.id, job.lease_token, ok=True,
                              counts=(pages, tables, figure_count))
        if status is None:
            return False, "租约已丢失，本次执行作废"
        return True, ""
    except queue.LeaseLost as exc:
        return False, str(exc)
    except P1Error as exc:
        queue.finish(job.id, job.lease_token, ok=False, error=str(exc))
        return False, str(exc)
    except Exception as exc:
        log.exception("摄入失败 doc=%s", job.id)
        queue.finish(job.id, job.lease_token, ok=False,
                     error=f"{type(exc).__name__}: {exc}")
        return False, f"{type(exc).__name__}: {exc}"


async def embed_doc_chunks(doc_id: int, lease_token: str, filename: str) -> int:
    """A7：seek_text 超过"文件名+2字"门槛的块批量向量化，BLOB 即真身。"""
    if not llm.available():
        log.info("未配置 DASHSCOPE_API_KEY，跳过向量化（关键词检索仍可用）")
        return 0
    with queue.lease_write(doc_id, lease_token) as db:
        rows = db.execute(
            "SELECT id, seek_text FROM chunks"
            " WHERE doc_id=? AND embedding IS NULL AND length(seek_text) > ?",
            (doc_id, len(filename) + 2)).fetchall()
    if not rows:
        return 0
    try:
        vectors = await llm.embed([r["seek_text"] for r in rows])
    except Exception:
        log.warning("向量化失败 doc=%s（块保留，稍后可手动重建）", doc_id, exc_info=True)
        return 0
    with queue.lease_write(doc_id, lease_token) as db:
        db.executemany(
            "UPDATE chunks SET embedding=? WHERE id=?",
            [(vector.vec_to_blob(v), r["id"]) for r, v in zip(rows, vectors)])
    return len(rows)


def _stored_upload(sha256: str, filename: str) -> Path:
    """按内容寻址找原件；找不到时报错（原件是重放的前提）。"""
    for ext in (".pdf", ".doc", ".docx"):
        candidate = upload_path(sha256, ext)
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"原件丢失：{filename}")


def upload_path(sha256: str, ext: str) -> Path:
    """内容寻址的上传路径——唯一命名约定（store_upload 与导入 API 共用，勿散落拼写）。"""
    return settings.upload_dir / f"{sha256[:16]}{ext}"


def store_upload(data: bytes, sha256: str, ext: str) -> Path:
    """A1 收件：内容寻址落盘（同文件零重复存储）。"""
    path = upload_path(sha256, ext)
    if not path.exists():
        path.write_bytes(data)
    return path
