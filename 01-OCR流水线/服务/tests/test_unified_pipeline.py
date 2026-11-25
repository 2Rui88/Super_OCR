"""统一文档链路回归（不加载 OCR 模型）。

- 全走 StructureV3：混合 PDF 不再按文字层分流，每页都进 OCR 引擎；
- Office → PDF 转换：产物复用、页数上限、失败/超时归一为 ConvertError（mock soffice）；
- 上传验真：Office 扩展名 + OOXML/OLE2 magic；
- 任务领取：文档类全局互斥（含旧类型兼容）、device 进缓存签名。
"""
from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.doc_convert import ConvertError, convert_office_to_pdf, ensure_pdf
from app.core.document_pipeline import (DOCUMENT_TASK_TYPES, PaddleDocumentEngine,
                                        document_engine_signature, process_document)
from app.core.task_store import TaskStore
from app.engine.paddle_card import PaddleCardConfig, mkldnn_validated


def _pdf_bytes(pages: int = 1) -> bytes:
    import pymupdf

    buf = io.BytesIO()
    with pymupdf.open() as doc:
        for i in range(pages):
            doc.new_page()
        doc.save(buf)
    return buf.getvalue()


class UnifiedDocumentTest(unittest.TestCase):
    """混合 PDF（文字页 + 扫描页）统一走 OCR：所有页进引擎，source_mode 恒为 ocr。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_mixed_pdf_routes_every_page_through_ocr(self):
        import pymupdf
        from app.contracts import Block, BlockType

        pdf = self.dir / "mixed.pdf"
        with pymupdf.open() as doc:
            page = doc.new_page()
            page.insert_text((72, 96), "第一页有足够的文字层内容，但统一链路下也走 OCR")
            doc.new_page()  # 第二页扫描页
            doc.save(pdf)

        seen = []
        def scan_reader(image, page_num):
            seen.append(page_num)
            return [Block(type=BlockType.TEXT, text=f"第{page_num}页识别结果",
                          bbox=[0, 0, 1, 1])]

        result = process_document(pdf, scan_reader=scan_reader)
        self.assertEqual(seen, [1, 2])                      # 无一页走文字层直读
        self.assertEqual([p.source_mode.value for p in result.pages], ["ocr", "ocr"])
        self.assertEqual(result.pages[0].blocks[0].text, "第1页识别结果")

    def test_empty_page_produces_quality_warning(self):
        from app.contracts import Block

        pdf = self.dir / "blank.pdf"
        pdf.write_bytes(_pdf_bytes(1))
        result = process_document(pdf, scan_reader=lambda img, n: [])
        self.assertEqual(result.quality_warnings, ["第1页未提取到文字或表格"])

    def test_on_page_callback_receives_real_page_result(self):
        """逐页上报的数据源：回调拿到的就是该页真实识别产物。"""
        from app.contracts import Block, BlockType

        pdf = self.dir / "twopage.pdf"
        pdf.write_bytes(_pdf_bytes(2))
        seen = []

        def scan_reader(image, page_num):
            return [Block(type=BlockType.TABLE if page_num == 1 else BlockType.TEXT,
                          text="x", bbox=[0, 0, 1, 1], html="<table></table>",
                          row_texts=["a | b"])]

        process_document(pdf, scan_reader=scan_reader,
                         on_page=lambda page, total: seen.append(
                             (page.page_num, total, len(page.blocks), page.duration_ms)))
        self.assertEqual([s[0] for s in seen], [1, 2])          # 页序正确
        self.assertEqual([s[1] for s in seen], [2, 2])          # 总数正确
        self.assertTrue(all(s[2] == 1 and s[3] >= 0 for s in seen))


class TableOrientationTest(unittest.TestCase):
    def test_rechecks_only_the_mistaken_half_turn_on_a_mixed_page(self):
        def layout(box, value):
            return {"block_label": "table", "block_bbox": box,
                    "block_content": f"<table><tr><td>{value}</td></tr></table>"}

        def half_turn(block):
            x, y, _, _ = block["block_bbox"]
            return {"pred_html": block["block_content"],
                    "cell_box_list": [[x+50, y+40, x+100, y+80], [x, y+40, x+50, y+80],
                                      [x+50, y, x+100, y+40], [x, y, x+50, y+40]]}

        upright = [0, 0, 100, 80]
        upside_down = [120, 0, 220, 80]
        first = [layout(upright, "误旋转"), layout(upside_down, "正确旋转")]
        text_boxes = [[x+10, y, x+40, y+10] for x in (0, 120) for y in (8, 30, 52)]
        initial = {"parsing_res_list": first, "table_res_list": [half_turn(b) for b in first],
                   "overall_ocr_res": {"textline_orientation_angles": [0, 0, 0, 1, 1, 1],
                                       "rec_boxes": text_boxes, "rec_scores": [0.99] * 6,
                                       "rec_texts": ["职级", "一类城市", "副高级"] * 2}}
        retry = {"parsing_res_list": [layout(upright, "恢复正向"),
                                     layout(upside_down, "不应采用的复读结果")]}
        engine = PaddleDocumentEngine()
        engine._pipeline = mock.Mock()
        engine._pipeline.predict.side_effect = [[initial], [retry]]
        page = mock.Mock(shape=(100, 240, 3))

        blocks = engine(page, 1)

        self.assertEqual([block.text for block in blocks], ["恢复正向", "正确旋转"])
        self.assertEqual(engine._pipeline.predict.call_args_list,
                         [mock.call(page), mock.call(page, use_table_orientation_classify=False)])


class DocConvertTest(unittest.TestCase):
    """mock soffice：只测编排逻辑（复用/页数上限/失败归一），不依赖真实 LibreOffice。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.src = self.dir / "abc123.docx"
        self.src.write_bytes(b"PK fake docx")
        self.out = self.dir / "converted"

    def tearDown(self):
        self.temp.cleanup()

    def _fake_soffice(self, pages=1, returncode=0):
        """伪 soffice：解析 --outdir 与源文件，在 outdir 落一份真 PDF。"""
        def run(cmd, **kwargs):
            outdir = Path(cmd[cmd.index("--outdir") + 1])
            src = Path(cmd[-1])
            outdir.mkdir(parents=True, exist_ok=True)
            (outdir / f"{src.stem}.pdf").write_bytes(_pdf_bytes(pages))
            return subprocess.CompletedProcess(cmd, returncode)
        return run

    def test_conversion_success_lands_in_out_dir(self):
        with mock.patch("app.core.doc_convert.subprocess.run",
                        side_effect=self._fake_soffice()):
            pdf = convert_office_to_pdf(self.src, self.out)
        self.assertEqual(pdf, self.out / "abc123.pdf")
        self.assertTrue(pdf.exists())

    def test_second_call_reuses_artifact_without_soffice(self):
        with mock.patch("app.core.doc_convert.subprocess.run",
                        side_effect=self._fake_soffice()):
            convert_office_to_pdf(self.src, self.out)
        with mock.patch("app.core.doc_convert.subprocess.run") as run:
            pdf = convert_office_to_pdf(self.src, self.out)
        run.assert_not_called()                      # 产物按内容哈希复用
        self.assertEqual(pdf, self.out / "abc123.pdf")

    def test_page_overflow_raises_doc_too_long(self):
        with mock.patch("app.core.doc_convert.subprocess.run",
                        side_effect=self._fake_soffice(pages=3)):
            with self.assertRaises(ConvertError) as ctx:
                convert_office_to_pdf(self.src, self.out, max_pages=2)
        self.assertEqual(ctx.exception.error_code, "DOC_TOO_LONG")

    def test_soffice_failure_raises_convert_failed(self):
        with mock.patch("app.core.doc_convert.subprocess.run",
                        side_effect=self._fake_soffice(returncode=1)):
            with self.assertRaises(ConvertError) as ctx:
                convert_office_to_pdf(self.src, self.out)
        self.assertEqual(ctx.exception.error_code, "CONVERT_FAILED")

    def test_missing_output_raises_convert_failed(self):
        with mock.patch("app.core.doc_convert.subprocess.run",
                        return_value=subprocess.CompletedProcess([], 0)):
            with self.assertRaises(ConvertError) as ctx:
                convert_office_to_pdf(self.src, self.out)
        self.assertEqual(ctx.exception.error_code, "CONVERT_FAILED")

    def test_timeout_raises_convert_failed(self):
        def hang(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 1)
        with mock.patch("app.core.doc_convert.subprocess.run", side_effect=hang):
            with self.assertRaises(ConvertError) as ctx:
                convert_office_to_pdf(self.src, self.out, timeout_seconds=1)
        self.assertEqual(ctx.exception.error_code, "CONVERT_FAILED")

    def test_ensure_pdf_passthrough_for_pdf(self):
        pdf = self.dir / "native.pdf"
        pdf.write_bytes(_pdf_bytes(1))
        result, converted = ensure_pdf(pdf, self.out)
        self.assertEqual((result, converted), (pdf, False))


class ClaimCapabilityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = TaskStore(Path(self.temp.name) / "tasks.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def _create(self, task_type, name):
        return self.store.create_task(task_type=task_type, filename=name,
                                      file_sha256=f"sha-{name}", file_path=f"/x/{name}")

    def test_worker_claims_legacy_queued_types(self):
        """旧库遗留的 document_text_layer queued 行仍可被领取处理。"""
        legacy = self._create("document_text_layer", "a.pdf")
        claimed = self.store.claim_next_task("w1", 1,
                                             claimable_types=["card", "document_scan",
                                                              "document", "document_text_layer"])
        self.assertEqual((claimed.id, claimed.task_type), (legacy, "document_text_layer"))

    def test_document_mutex_covers_all_document_types(self):
        """document_scan 在跑时，document_text_layer 也被互斥挡住（同一文档类）。"""
        scan = self._create("document_scan", "s.pdf")
        self._create("document_text_layer", "t.pdf")
        self.store.claim_next_task("w1", 1, claimable_types=["document_scan"])
        self.assertIsNone(self.store.claim_next_task(
            "w2", 1, claimable_types=["document_text_layer"]))
        self.store.finish_task(scan, result_json="{}", engine_type="structure_v3")
        self.assertIsNotNone(self.store.claim_next_task(
            "w2", 1, claimable_types=["document_text_layer"]))

    def test_document_task_types_constant(self):
        self.assertEqual(DOCUMENT_TASK_TYPES,
                         ("document", "document_text_layer", "document_scan"))


class DeviceConfigTest(unittest.TestCase):
    def test_device_rejects_invalid_value(self):
        with self.assertRaises(ValueError):
            PaddleCardConfig(device="tpu")
        PaddleCardConfig(device="gpu:0")  # 合法

    def test_device_changes_cache_signatures(self):
        cpu_card = PaddleCardConfig(device="cpu")
        gpu_card = PaddleCardConfig(device="gpu:0")
        self.assertNotEqual(_config_signature(cpu_card), _config_signature(gpu_card))
        self.assertNotEqual(document_engine_signature(device="cpu"),
                            document_engine_signature(device="gpu:0"))

    def test_mkldnn_disabled_on_gpu(self):
        self.assertFalse(mkldnn_validated("gpu:0"))
        self.assertFalse(mkldnn_validated("gpu"))


def _config_signature(config: PaddleCardConfig) -> str:
    """离线比较 config 序列化（不实例化引擎）：device 必须进签名。"""
    import dataclasses, hashlib, json
    payload = dataclasses.asdict(config)
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


if __name__ == "__main__":
    unittest.main()
