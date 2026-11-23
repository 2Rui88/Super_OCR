"""领取调度与设备参数化的离线回归(不加载 OCR 模型)。

适配统一 worker 设计(双 GPU worker 同质领取全部任务,文档统一 document_scan):
- 领取:证件优先于文档类;能力过滤参数仍受支持(worker 传全量列表);
- device 进缓存签名:CPU/GPU 结果缓存天然分离;GPU 下不再启用 mkldnn。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.document_pipeline import (DOCUMENT_TASK_TYPES,
                                        document_engine_signature)
from app.core.task_store import TaskStore
from app.engine.paddle_card import PaddleCardConfig, mkldnn_validated


class ClaimSchedulingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = TaskStore(Path(self.temp.name) / "tasks.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def _create(self, task_type, name):
        return self.store.create_task(task_type=task_type, filename=name,
                                      file_sha256=f"sha-{name}", file_path=f"/x/{name}")

    def test_card_jumps_queue_ahead_of_older_documents(self):
        doc = self._create("document_scan", "d.pdf")
        card = self._create("card", "c.jpg")
        claimed = self.store.claim_next_task("w1", 1, claimable_types=[
            "card", "document_scan", "document", "document_text_layer"])
        self.assertEqual((claimed.id, claimed.task_type), (card, "card"))
        self.assertEqual(self.store.claim_next_task("w1", 1, claimable_types=[
            "card", "document_scan", "document"]).id, doc)

    def test_document_mutex_covers_all_document_types(self):
        scan = self._create("document_scan", "s.pdf")
        text = self._create("document_text_layer", "t.pdf")
        all_types = ["card", "document_scan", "document", "document_text_layer"]
        self.store.claim_next_task("w1", 1, claimable_types=all_types)
        # 文档类全局并发 1:第二个文档类任务被互斥挡住
        self.assertIsNone(self.store.claim_next_task("w2", 1, claimable_types=all_types))
        self.store.finish_task(scan, result_json="{}", engine_type="structure_v3")
        self.assertEqual(self.store.claim_next_task("w2", 1, claimable_types=all_types).id, text)

    def test_capability_filter_still_supported(self):
        text = self._create("document_text_layer", "a.pdf")
        self._create("card", "c.jpg")
        claimed = self.store.claim_next_task("legacy-cpu", 1,
                                             claimable_types=["document_text_layer"])
        self.assertEqual(claimed.task_type, "document_text_layer")
        self.assertIsNone(self.store.claim_next_task("legacy-cpu", 1,
                                                     claimable_types=["document_text_layer"]))

    def test_document_task_types_constant(self):
        self.assertEqual(DOCUMENT_TASK_TYPES,
                         ("document", "document_text_layer", "document_scan"))


class DeviceConfigTest(unittest.TestCase):
    def test_device_rejects_invalid_value(self):
        with self.assertRaises(ValueError):
            PaddleCardConfig(device="tpu")
        PaddleCardConfig(device="gpu:0")  # 合法

    def test_device_changes_cache_signatures(self):
        import dataclasses, hashlib, json

        def sig(cfg):
            return hashlib.sha256(json.dumps(
                dataclasses.asdict(cfg), sort_keys=True).encode()).hexdigest()

        self.assertNotEqual(sig(PaddleCardConfig(device="cpu")),
                            sig(PaddleCardConfig(device="gpu:0")))
        self.assertNotEqual(document_engine_signature(device="cpu"),
                            document_engine_signature(device="gpu:0"))

    def test_mkldnn_disabled_on_gpu(self):
        self.assertFalse(mkldnn_validated("gpu:0"))
        self.assertFalse(mkldnn_validated("gpu"))


if __name__ == "__main__":
    unittest.main()
