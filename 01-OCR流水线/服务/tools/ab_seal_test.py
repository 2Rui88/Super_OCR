"""PP-StructureV3 印章识别 A/B：同一批 PDF，seal 开/关对比验证功能是否生效。

对每份 PDF 逐页跑两种配置（use_seal_recognition False/True，其余选项与
生产线 _STRUCTURE_OPTIONS 完全一致），收集 parsing_res_list 的
(label, content)，重点看 seal 块是否出现、章内文字是否读出、耗时差异。

设备：--device 或环境变量 OCR_DEVICE，默认 cpu。
GPU 服务器上（服务目录）：
    .venv/bin/python tools/ab_seal_test.py --device gpu:0
首次开启 seal 会从官方源下载印章检测/识别模型。

产物：tests/_out/seal_test/report.json（--out 覆盖）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PDF_DIR = ROOT / "tests" / "_out" / "seal_test"


def render_pages(pdf: Path, dpi: int = 144):
    """PDF 逐页渲染为 BGR ndarray（与 process_document 同口径）。"""
    import numpy as np
    import pymupdf

    with pymupdf.open(pdf) as doc:
        for page in doc:
            pix = page.get_pixmap(dpi=dpi, alpha=False)
            rgb = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
                pix.height, pix.width, 3)
            yield page.number + 1, rgb[:, :, ::-1].copy()


def run_config(pages, *, seal_on: bool, device: str) -> list[dict]:
    from paddleocr import PPStructureV3

    from app.core.document_pipeline import _STRUCTURE_OPTIONS

    options = {**_STRUCTURE_OPTIONS, "use_seal_recognition": seal_on}
    kwargs = {"device": device}
    if device == "cpu":
        from app.engine.paddle_card import mkldnn_validated
        kwargs.update(cpu_threads=4, enable_mkldnn=mkldnn_validated())
    pipeline = PPStructureV3(**kwargs, **options)
    results = []
    for page_num, bgr in pages:
        started = time.perf_counter()
        items, result_keys = [], []
        for res in pipeline.predict(bgr):
            try:
                result_keys = [str(k) for k in res.keys()]
            except Exception:
                pass
            for item in (res.get("parsing_res_list") or []):
                if isinstance(item, dict):
                    label = str(item.get("block_label"))
                    content = str(item.get("block_content") or "")
                else:
                    label = str(item.label)
                    content = str(item.content or "")
                items.append({"label": label, "content": content.strip()})
        results.append({"page": page_num,
                        "duration_ms": round((time.perf_counter() - started) * 1000),
                        "result_keys": result_keys,
                        "items": items})
    pipeline.close()
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default=os.environ.get("OCR_DEVICE", "cpu"),
                        help="cpu | gpu | gpu:0（默认读 OCR_DEVICE，缺省 cpu）")
    parser.add_argument("--pdf-dir", type=Path, default=PDF_DIR)
    parser.add_argument("--out", type=Path, default=PDF_DIR / "report.json")
    args = parser.parse_args()

    pdfs = sorted(args.pdf_dir.glob("*.pdf"))
    if not pdfs:
        print(f"未找到 PDF：{args.pdf_dir}（先跑 tools/gen_seal_pdfs.py）")
        return 1

    report = {"device": args.device, "generated_at": datetime.now().isoformat(),
              "pdf_dir": str(args.pdf_dir), "files": {}}
    for pdf in pdfs:
        pages = list(render_pages(pdf))
        off = run_config(pages, seal_on=False, device=args.device)
        on = run_config(pages, seal_on=True, device=args.device)
        report["files"][pdf.name] = {"seal_off": off, "seal_on": on}

        seals = [i for r in on for i in r["items"] if i["label"] == "seal"]
        ms_off = sum(r["duration_ms"] for r in off)
        ms_on = sum(r["duration_ms"] for r in on)
        print(f"\n== {pdf.name}（{len(pages)} 页，device={args.device}）==")
        print(f"  seal=off  合计 {ms_off}ms | seal=on 合计 {ms_on}ms")
        if seals:
            for s in seals:
                print(f"  [seal] {s['content']!r}")
        else:
            print("  [seal] 未检出任何印章块")
        # off 配置下印章区域文字是否被混入普通文本（对照）
        off_text = "".join(i["content"] for r in off for i in r["items"])
        leaked = "澄川市恒信科技" in off_text.replace(" ", "")
        print(f"  对照：seal=off 时章名混入正文文本 → {'是' if leaked else '否'}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"\n→ {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
