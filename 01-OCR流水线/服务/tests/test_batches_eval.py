"""批次/重试/导出/评测的离线回归 —— 不加载 OCR 模型，worker 行为由 TaskStore 模拟。"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.core.evaluation import evaluate_batch
from app.core.task_store import TaskStore


def _png_bytes() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 200, 200)).save(buf, format="PNG")
    return buf.getvalue()


def _card_envelope(task_id: int, doc_type="id_front", fields=None, flagged=()):
    """构造 worker 写回的统一信封（kind=card）。"""
    fields = fields or {}
    result = {
        "file_sha256": f"sha-{task_id}", "filename": f"file{task_id}.jpg",
        "doc_type": doc_type, "doc_type_cn": "身份证正面", "type_confidence": 1.0,
        "fields": [
            {"name": name, "value": value, "confidence": 0.99, "bbox": None,
             "validation": {"passed": name not in flagged,
                            "errors": ["校验位错误"] if name in flagged else []},
             "needs_review": name in flagged}
            for name, value in fields.items()
        ],
        "issues": [], "duration_ms": 100,
    }
    return {"kind": "card", "reason": "", "result": result}


class BatchRetryExportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.settings = Settings(task_db_path=base / "tasks.sqlite3",
                                 cache_db_path=base / "cache.sqlite3",
                                 upload_dir=base / "uploads",
                                 cert_dir=ROOT / "config" / "certs")
        from app.api.app import create_app
        from fastapi.testclient import TestClient

        self.store = TaskStore(self.settings.task_db_path)
        self.client = TestClient(create_app(self.settings))

    def tearDown(self):
        self.temp.cleanup()

    def _run_worker_once(self):
        """模拟 worker：领取一个任务并按内容成功/失败写回。"""
        task = self.store.claim_next_task("w-test")
        if task is None:
            return
        if task.filename == "poison.jpg":  # 毒丸：永远失败（存储路径是哈希名，看原始名）
            self.store.fail_task(task.id, error_code="WORKER_ERROR", error_message="boom")
        else:
            envelope = _card_envelope(
                task.id, fields={"姓名": "张三", "公民身份号码": "999901199603146917"},
                flagged=("公民身份号码",))
            self.store.finish_task(task.id,
                                   result_json=json.dumps(envelope, ensure_ascii=False),
                                   engine_type="card_ocr")

    def test_batch_isolation_retry_and_aggregation(self):
        good = [("a.jpg", _png_bytes()), ("poison.jpg", _png_bytes()),
                ("c.jpg", _png_bytes()), ("bad.jpg", b"not an image")]
        response = self.client.post(
            "/api/v1/batches",
            files=[("files", (name, content, "image/jpeg")) for name, content in good])
        body = response.json()
        self.assertEqual((response.status_code, body["accepted"], body["rejected"]),
                         (202, 3, 1))
        batch_id = body["batch_id"]

        for _ in range(3):
            self._run_worker_once()
        overview = self.client.get(f"/api/v1/batches/{batch_id}").json()
        self.assertEqual((overview["total"], overview["counts"]["done"],
                          overview["counts"]["failed"]), (4, 2, 2))
        failed = {item["filename"]: item["error_code"] for item in overview["items"]
                  if item["status"] == "failed"}
        self.assertEqual(failed, {"poison.jpg": "WORKER_ERROR",
                                  "bad.jpg": "UNSUPPORTED_FORMAT"})

        # 重试：毒丸第 1 次重排（attempts=2<3），上传被拒的直接跳过
        retry = self.client.post(f"/api/v1/batches/{batch_id}/retry_failed").json()
        self.assertEqual((retry["requeued"], retry["skipped_permanent"]), (1, 1))
        self._run_worker_once()
        # 毒丸第 2 次重排（attempts=3）→ 到顶，永久失败
        self.assertEqual(self.client.post(
            f"/api/v1/batches/{batch_id}/retry_failed").json(),
            {"requeued": 1, "skipped_permanent": 1})
        self._run_worker_once()
        final = self.client.post(f"/api/v1/batches/{batch_id}/retry_failed").json()
        self.assertEqual((final["requeued"], final["skipped_permanent"]), (0, 2))

    def test_export_xlsx_writes_numbers_as_text(self):
        response = self.client.post(
            "/api/v1/batches",
            files=[("files", ("a.jpg", _png_bytes(), "image/jpeg"))])
        batch_id = response.json()["batch_id"]
        self._run_worker_once()

        from openpyxl import load_workbook

        export = self.client.get(f"/api/v1/batches/{batch_id}/export",
                                 params={"format": "xlsx"})
        self.assertEqual(export.status_code, 200)
        self.assertIn("spreadsheetml", export.headers["content-type"])
        book = load_workbook(io.BytesIO(export.content))
        sheet = book.active
        header = [cell.value for cell in sheet[1]]
        id_column = header.index("公民身份号码")
        cell = sheet[2][id_column]
        self.assertEqual(cell.value, "999901199603146917")  # 18 位完整
        self.assertEqual(cell.number_format, "@")           # 文本格式
        self.assertEqual(sheet[2][header.index("姓名")].value, "张三")

    def test_export_json_contains_full_envelope(self):
        response = self.client.post(
            "/api/v1/batches",
            files=[("files", ("a.jpg", _png_bytes(), "image/jpeg"))])
        batch_id = response.json()["batch_id"]
        self._run_worker_once()
        export = self.client.get(f"/api/v1/batches/{batch_id}/export",
                                 params={"format": "json"})
        payload = export.json()
        self.assertEqual(payload["items"][0]["result"]["kind"], "card")
        self.assertEqual(payload["counts"]["done"], 1)
        bad = self.client.get(f"/api/v1/batches/{batch_id}/export",
                              params={"format": "csv"})
        self.assertEqual(bad.status_code, 422)

    def test_eval_endpoint_reports_accuracy_and_detection(self):
        truth = [{
            "file": "a.jpg", "doc": "id_front", "doc_cn": "身份证正面",
            "fields": {"姓名": "张三", "公民身份号码": "999901199603146917"},
            "invalid_fields": ["公民身份号码"],   # 号码本来就错，应该被标出
        }, {
            "file": "missing.jpg", "doc": "id_front", "doc_cn": "身份证正面",
            "fields": {"姓名": "李四"},
        }]
        response = self.client.post("/api/v1/batches",
                                    files=[("files", ("a.jpg", _png_bytes(), "image/jpeg"))])
        batch_id = response.json()["batch_id"]
        self._run_worker_once()
        result = self.client.post("/api/v1/eval", data={"batch_id": batch_id},
                                  files={"ground_truth": ("gt.json", json.dumps(truth).encode(), "application/json")})
        body = result.json()
        self.assertEqual(result.status_code, 200)
        report = body["report"]
        self.assertEqual(report["field_accuracy"], 1.0)          # 姓名+号码值都对
        self.assertEqual(report["planted_invalid_flag_rate"], 1.0)  # 错号码被标出
        self.assertEqual(report["pending_files"], ["missing.jpg"])   # 不在批次的文件列出
        self.assertEqual(body["batch_id"], batch_id)
        self.assertEqual(len(body["ground_truth_sha256"]), 64)

        broken = self.client.post("/api/v1/eval", data={"batch_id": batch_id},
                                  files={"ground_truth": ("gt.json", b"{not json", "application/json")})
        self.assertEqual(broken.status_code, 422)

    def test_missing_batch_returns_404(self):
        for path, method in (("/api/v1/batches/99", "get"),
                             ("/api/v1/batches/99/retry_failed", "post")):
            response = getattr(self.client, method)(path)
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json()["detail"]["error_code"], "BATCH_NOT_FOUND")


class EvaluateLogicTest(unittest.TestCase):
    def test_classification_miss_counts_all_fields_wrong(self):
        tasks = [{"filename": "x.jpg", "status": "done",
                  "result_json": json.dumps({"kind": "document", "reason": "不是证件，已按文档识别",
                                             "result": {"pages": []}})}]
        truth = [{"file": "x.jpg", "doc": "id_front", "doc_cn": "身份证正面",
                  "fields": {"姓名": "张三", "性别": "男"}}]
        report = evaluate_batch(tasks, truth)
        self.assertEqual((report["correct_fields"], report["total_fields"]), (0, 2))
        self.assertEqual(report["field_accuracy"], 0.0)
        self.assertEqual(report["type_accuracy"], 0.0)
        self.assertEqual(report["wrong_field_flag_rate"], 0.0)  # 2 个错字段 0 个被标出

    def test_classification_miss_not_counted_as_planted_detection(self):
        # 分类判错属于分类失败，没走到字段级校验，不计入"故意做错检出率"分母
        tasks = [{"filename": "x.jpg", "status": "done",
                  "result_json": json.dumps({"kind": "document", "reason": "不是证件，已按文档识别",
                                             "result": {"pages": []}})}]
        truth = [{"file": "x.jpg", "doc": "id_front", "doc_cn": "身份证正面",
                  "fields": {"姓名": "张三"}, "invalid_fields": ["姓名"]}]
        report = evaluate_batch(tasks, truth)
        self.assertEqual(report["planted_invalid_total"], 0)
        self.assertEqual(report["planted_invalid_flag_rate"], None)


if __name__ == "__main__":
    unittest.main()
