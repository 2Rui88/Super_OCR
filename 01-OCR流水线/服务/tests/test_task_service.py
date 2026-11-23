"""任务库与 HTTP 接口的离线回归：领取互斥、恢复、心跳收割、上传安全、路由信封。

不加载任何 OCR 模型：worker 行为由测试直接操作 TaskStore 模拟。
"""
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
from app.core.image_routing import FALLBACK_REASON, process_image
from app.core.task_store import TaskStore
from app.engine.base import BaseCardEngine, OcrLine


def _png_bytes() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 200, 200)).save(buf, format="PNG")
    return buf.getvalue()


def _write_image(path: Path) -> Path:
    path.write_bytes(_png_bytes())
    return path


class TaskStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = TaskStore(Path(self.temp.name) / "tasks.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def _create(self, task_type="card", name="a.jpg"):
        return self.store.create_task(task_type=task_type, filename=name,
                                      file_sha256=f"sha-{name}", file_path=f"/x/{name}")

    def test_claim_follows_fifo_and_counts_attempts(self):
        first = self._create(name="a.jpg")
        second = self._create(name="b.jpg")
        claimed = self.store.claim_next_task("w1")
        self.assertEqual(claimed.id, first)
        task = self.store.get_task(first)
        self.assertEqual((task["status"], task["worker_id"], task["attempts"]),
                         ("running", "w1", 1))
        # 完成后才能领到下一个
        self.assertEqual(self.store.claim_next_task("w1").id, second)
        self.store.finish_task(first, result_json="{}", engine_type="card_ocr")
        self.assertEqual(self.store.get_task(first)["status"], "done")
        self.assertIsNone(self.store.claim_next_task("w1"))  # 无可领任务

    def test_document_tasks_are_globally_mutually_exclusive(self):
        doc1 = self._create("document", "d1.pdf")
        self._create("document", "d2.pdf")
        card = self._create("card", "c.jpg")
        # 证件优先：card 先于更老的文档被领取（长 PDF 不应挡住证件）
        claimed = self.store.claim_next_task("w1")
        self.assertEqual((claimed.id, claimed.task_type), (card, "card"))
        self.assertEqual(self.store.claim_next_task("w2").id, doc1)
        # d2 被互斥挡住
        self.assertIsNone(self.store.claim_next_task("w1"))
        self.store.finish_task(doc1, result_json="{}", engine_type="structure_v3")
        self.assertEqual(self.store.claim_next_task("w1").task_type, "document")

    def test_document_concurrency_limit_is_configurable(self):
        # OCR_MAX_RUNNING_DOCUMENTS 对应 claim 参数：调高后允许多个文档并行
        for name in ("d1.pdf", "d2.pdf", "d3.pdf"):
            self._create("document", name)
        card = self._create(name="c.jpg")
        self.assertEqual(self.store.claim_next_task("w1", max_running_documents=2).id, card)
        self.assertIsNotNone(self.store.claim_next_task("w2", max_running_documents=2))
        self.assertIsNotNone(self.store.claim_next_task("w1", max_running_documents=2))
        # 两个文档在跑：第三个文档被挡，也没有别的可领
        self.assertIsNone(self.store.claim_next_task("w1", max_running_documents=2))

    def test_requeue_only_failed_and_progress_only_running(self):
        task_id = self._create()
        self.store.claim_next_task("w1")
        self.assertFalse(self.store.requeue_task(task_id))               # running 不可重排
        self.store.fail_task(task_id, error_code="WORKER_ERROR", error_message="boom")
        self.assertTrue(self.store.requeue_task(task_id))
        self.assertEqual(self.store.get_task(task_id)["status"], "queued")
        self.store.finish_task(task_id, result_json="{}", engine_type="card_ocr")
        self.assertFalse(self.store.requeue_task(task_id))               # done 不可重排

    def test_recover_and_reap_only_touch_own_running_tasks(self):
        mine = self._create()
        other = self._create(name="b.jpg")
        self.store.claim_next_task("w1")
        self.store.claim_next_task("w2")
        self.assertEqual(self.store.recover_worker_tasks(
            "w1", error_code="SERVICE_RESTARTED", error_message="x"), 1)
        self.assertEqual(self.store.get_task(mine)["error_code"], "SERVICE_RESTARTED")
        self.assertEqual(self.store.get_task(other)["status"], "running")  # 别人的不动

        self.store.heartbeat("w2", pid=1, card_model_loaded=True,
                             document_model_loaded=False, current_task_id=other)
        with self.store._connect() as db:  # 把心跳拨回过去，模拟失联
            db.execute("UPDATE workers SET last_heartbeat_at='2020-01-01T00:00:00+00:00'")
        self.assertEqual(self.store.reap_stale_workers(stale_seconds=30), ["w2"])
        self.assertEqual(self.store.get_task(other)["error_code"], "WORKER_LOST")
        workers = self.store.list_workers(stale_seconds=30)
        self.assertEqual([(w["worker_id"], w["alive"]) for w in workers],
                         [("w2", False)])

    def test_healthcheck_writes_and_cleans(self):
        self.assertTrue(self.store.healthcheck())

    def test_pages_progress_roundtrip_and_reset_on_claim(self):
        task_id = self._create("document_scan", "d.pdf")
        self.store.claim_next_task("w1", claimable_types=["document_scan"])
        pages = [{"page": 1, "blocks": 12, "tables": 1, "duration_ms": 900, "warning": None}]
        self.store.update_progress(task_id, "识别中 第1/3页", 1 / 3,
                                   pages_progress={"total": 3, "pages": pages})
        task = self.store.get_task(task_id)
        stored = json.loads(task["pages_progress"])   # 库里是 JSON 文本，API 层解析
        self.assertEqual(stored["total"], 3)
        self.assertEqual(stored["pages"][0]["tables"], 1)
        # 重新排队再领取：逐页进度清零，不残留上一轮状态
        self.store.fail_task(task_id, error_code="WORKER_ERROR", error_message="x")
        self.store.requeue_task(task_id)
        self.store.claim_next_task("w2", claimable_types=["document_scan"])
        self.assertIsNone(self.store.get_task(task_id)["pages_progress"])

    def test_pages_progress_column_added_to_legacy_db(self):
        """旧库无 pages_progress 列：初始化自动补列，既有任务照常读取。"""
        import sqlite3

        legacy = Path(self.temp.name) / "legacy.sqlite3"
        db = sqlite3.connect(legacy)
        try:
            with db:
                db.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, task_type TEXT,"
                           " status TEXT, phase TEXT, progress REAL, filename TEXT,"
                           " file_sha256 TEXT, file_path TEXT, hint_type TEXT, engine_type TEXT,"
                           " result_json TEXT, error_code TEXT, error_message TEXT,"
                           " attempts INTEGER, worker_id TEXT, created_at TEXT,"
                           " started_at TEXT, finished_at TEXT)")
                db.execute("INSERT INTO tasks(task_type, status, filename, file_sha256,"
                           " file_path, created_at) VALUES ('document','done','a.pdf','s','/x','t')")
        finally:
            db.close()
        store = TaskStore(legacy)  # 迁移在构造时发生
        self.assertIsNone(store.get_task(1)["pages_progress"])
        store.create_task(task_type="document_scan", filename="b.pdf",
                          file_sha256="s2", file_path="/x/b.pdf")


class ImageRoutingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        from app.certs import load_certs
        self.registry = load_certs(ROOT / "config" / "certs")

    def tearDown(self):
        self.temp.cleanup()

    def _engine(self, lines):
        class Fixture(BaseCardEngine):
            cache_signature = "fixture"
            def recognize_lines(self, path):
                return [OcrLine(text, bbox) for text, bbox in lines]
        return Fixture()

    def _image(self):
        return _write_image(self.path / "x.jpg")

    def test_cert_image_reuses_lines_without_second_ocr(self):
        image = self._image()
        engine = self._engine([("居民身份证", [0.05, 0.05, 0.4, 0.10]),
                               ("公民身份号码999901199603146917", [0.05, 0.8, 0.9, 0.85])])
        seen = []
        envelope = process_image(image, card_engine=engine,
                                 document_reader=lambda img, n: seen.append(n) or [],
                                 registry=self.registry)
        self.assertEqual(envelope["kind"], "card")
        self.assertEqual(envelope["result"]["doc_type"], "id_front")
        self.assertEqual(seen, [])                                       # 未触发文档兜底

    def test_non_cert_image_falls_back_to_document(self):
        from app.contracts import Block, BlockType
        image = self._image()
        engine = self._engine([("第一章 总则", [0.1, 0.1, 0.5, 0.15])])
        envelope = process_image(image, card_engine=engine,
                                 document_reader=lambda img, n: [
                                     Block(type=BlockType.TEXT, text="文档文字",
                                           bbox=[0, 0, 1, 1])],
                                 registry=self.registry)
        self.assertEqual(envelope["kind"], "document")
        self.assertEqual(envelope["reason"], FALLBACK_REASON)
        page = envelope["result"]["pages"][0]
        self.assertEqual((page["source_mode"], page["blocks"][0]["text"]),
                         ("ocr", "文档文字"))

    def test_hint_forces_card_path(self):
        image = self._image()
        engine = self._engine([("完全无关的文字", [0.1, 0.1, 0.5, 0.15])])
        envelope = process_image(image, card_engine=engine,
                                 document_reader=lambda img, n: [],
                                 registry=self.registry, hint_type="license")
        self.assertEqual(envelope["kind"], "card")
        self.assertEqual(envelope["result"]["doc_type"], "license")


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.settings = Settings(task_db_path=base / "tasks.sqlite3",
                                 cache_db_path=base / "cache.sqlite3",
                                 upload_dir=base / "uploads",
                                 cert_dir=ROOT / "config" / "certs",
                                 sync_wait_seconds=0.2)
        from app.api.app import create_app
        from fastapi.testclient import TestClient
        self.store = TaskStore(self.settings.task_db_path)
        self.client = TestClient(create_app(self.settings))

    def tearDown(self):
        self.temp.cleanup()

    def test_card_upload_then_worker_completes_via_store(self):
        response = self.client.post("/api/v1/cards", files={
            "file": ("sample.jpg", _png_bytes(), "image/jpeg")},
            data={"wait": "false"})
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]

        # 模拟 worker：领取 → 写回统一信封
        claimed = self.store.claim_next_task("w1")
        self.assertEqual(claimed.id, task_id)
        envelope = {"kind": "card", "reason": "",
                    "result": {"file_sha256": "x", "filename": "sample.jpg",
                               "doc_type": "id_front", "fields": []}}
        self.store.finish_task(task_id, result_json=json.dumps(envelope),
                               engine_type="card_ocr")

        detail = self.client.get(f"/api/v1/tasks/{task_id}").json()
        self.assertEqual((detail["status"], detail["engine_type"],
                          detail["result"]["kind"]), ("done", "card_ocr", "card"))

    def test_card_sync_wait_returns_202_when_worker_absent(self):
        response = self.client.post("/api/v1/cards", files={
            "file": ("sample.jpg", _png_bytes(), "image/jpeg")})
        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertEqual(body["status"], "running")
        self.assertIn("task_id", body)

    def test_upload_validation_rejects_disguised_files(self):
        response = self.client.post("/api/v1/cards", files={
            "file": ("evil.jpg", b"MZ not an image", "image/jpeg")},
            data={"wait": "false"})
        self.assertEqual(response.status_code, 415)
        self.assertEqual(response.json()["detail"]["error_code"], "UNSUPPORTED_FORMAT")

        response = self.client.post("/api/v1/documents", files={
            "file": ("evil.pdf", b"not a pdf", "application/pdf")})
        self.assertEqual(response.status_code, 415)

        response = self.client.post("/api/v1/cards", files={
            "file": ("notes.txt", b"hello", "text/plain")})
        self.assertEqual(response.status_code, 415)

    def test_oversized_upload_rejected_at_read_stage(self):
        # 限长读取：超过 20MB 上限在 read 阶段即 413，内容校验都不该走到
        payload = b"\x89PNG\r\n\x1a\n" + b"x" * (20 * 1024 * 1024 + 16)
        response = self.client.post("/api/v1/cards", files={
            "file": ("big.jpg", payload, "image/jpeg")}, data={"wait": "false"})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["detail"]["error_code"], "FILE_TOO_LARGE")

        # 混在批次里同样被逐文件拒绝，不影响其他文件
        response = self.client.post("/api/v1/batches", files=[
            ("files", ("big.jpg", payload, "image/jpeg")),
            ("files", ("ok.jpg", _png_bytes(), "image/jpeg"))])
        body = response.json()
        self.assertEqual(response.status_code, 202)
        self.assertEqual((body["accepted"], body["rejected"]), (1, 1))

    def test_upload_stores_by_content_hash_with_original_name_kept(self):
        self.client.post("/api/v1/cards", files={
            "file": ("我的证件.jpg", _png_bytes(), "image/jpeg")}, data={"wait": "false"})
        task = self.store.get_task(1)
        self.assertEqual(task["filename"], "我的证件.jpg")
        stored = Path(task["file_path"])
        self.assertEqual(stored.parent, self.settings.upload_dir)
        self.assertNotIn("我的证件", stored.name)  # 磁盘路径与用户文件名无关

    def test_document_upload_is_always_async(self):
        response = self.client.post("/api/v1/documents", files={
            "file": ("doc.pdf", b"%PDF-1.4 minimal", "application/pdf")})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "queued")

    def test_task_detail_exposes_pages_progress(self):
        """GET /tasks 带 pages_progress：前端逐页状态的数据源。"""
        task_id = self.store.create_task(task_type="document_scan", filename="d.pdf",
                                         file_sha256="s", file_path="/x/d.pdf")
        self.store.claim_next_task("w1", claimable_types=["document_scan"])
        self.store.update_progress(task_id, "识别中 第1/2页", 0.5,
                                   pages_progress={"total": 2, "pages": [
                                       {"page": 1, "blocks": 8, "tables": 0,
                                        "duration_ms": 700, "warning": None}]})
        detail = self.client.get(f"/api/v1/tasks/{task_id}").json()
        self.assertEqual(detail["pages_progress"]["total"], 2)
        self.assertEqual(detail["pages_progress"]["pages"][0]["page"], 1)
        # 无逐页进度的任务（如证件）字段为 None，不缺字段
        card_id = self.store.create_task(task_type="card", filename="c.jpg",
                                         file_sha256="s", file_path="/x/c.jpg")
        self.assertIsNone(self.client.get(f"/api/v1/tasks/{card_id}").json()["pages_progress"])

    def test_office_uploads_are_accepted_and_queued_as_scan(self):
        # OOXML（ZIP 容器）与 OLE2 头分别验真；任务统一为 document_scan
        for name, magic in (("doc.docx", b"PK\x03\x04docx"),
                            ("sheet.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1xls")):
            response = self.client.post("/api/v1/documents", files={
                "file": (name, magic, "application/octet-stream")})
            self.assertEqual(response.status_code, 202, name)
            self.assertEqual(self.store.get_task(response.json()["task_id"])["task_type"],
                             "document_scan")

    def test_disguised_office_files_are_rejected(self):
        for name, payload in (("evil.docx", b"MZ not a zip"),
                              ("evil.xls", b"PK but claims xls")):
            response = self.client.post("/api/v1/documents", files={
                "file": (name, payload, "application/octet-stream")})
            self.assertEqual(response.status_code, 415, name)
            self.assertEqual(response.json()["detail"]["error_code"],
                             "UNSUPPORTED_FORMAT")

    def test_health_reports_degraded_without_workers(self):
        body = self.client.get("/api/v1/health").json()
        self.assertEqual((body["status"], body["components"]["sqlite_writable"],
                          body["components"]["workers"]), ("degraded", True, []))

    def test_missing_task_returns_404_with_code(self):
        response = self.client.get("/api/v1/tasks/999")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["error_code"], "TASK_NOT_FOUND")

    def test_uploaded_file_served_readonly_for_history_review(self):
        """历史回看原图：上传件按内容哈希落盘后可只读取回。"""
        import hashlib

        response = self.client.post("/api/v1/cards", files={
            "file": ("sample.jpg", _png_bytes(), "image/jpeg")}, data={"wait": "false"})
        task = self.store.get_task(response.json()["task_id"])
        name = Path(task["file_path"]).name
        self.assertEqual(name, f"{hashlib.sha256(_png_bytes()).hexdigest()}.jpg")

        served = self.client.get(f"/api/v1/files/{name}")
        self.assertEqual(served.status_code, 200)
        self.assertEqual(served.content, _png_bytes())

    def test_file_endpoint_rejects_bad_names(self):
        for bad in ("../tasks.sqlite3", "evil.jpg", f"{'a'*64}.exe",
                    f"{'g'*64}.jpg", "no-ext"):
            response = self.client.get(f"/api/v1/files/{bad}")
            self.assertIn(response.status_code, (400, 404), bad)

    def test_file_endpoint_404_for_missing_content(self):
        response = self.client.get(f"/api/v1/files/{'0'*64}.jpg")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["error_code"], "FILE_NOT_FOUND")

    def _upload_pdf(self, name="doc.pdf", pages=2):
        import pymupdf

        buf = io.BytesIO()
        with pymupdf.open() as document:
            for i in range(pages):
                document.new_page()
            document.save(buf)
        response = self.client.post("/api/v1/documents",
                                    files={"file": (name, buf.getvalue(), "application/pdf")})
        self.assertEqual(response.status_code, 202)
        import hashlib

        return hashlib.sha256(buf.getvalue()).hexdigest()

    def test_page_image_rendered_from_pdf(self):
        sha = self._upload_pdf(pages=2)
        page1 = self.client.get(f"/api/v1/files/{sha}/page/1")
        self.assertEqual(page1.status_code, 200)
        self.assertEqual(page1.headers["content-type"], "image/png")
        self.assertTrue(page1.content.startswith(b"\x89PNG"))
        # 越界页码：404 + 语义化错误码
        beyond = self.client.get(f"/api/v1/files/{sha}/page/3")
        self.assertEqual(beyond.status_code, 404)
        self.assertEqual(beyond.json()["detail"]["error_code"], "PAGE_NOT_FOUND")

    def test_page_image_prefers_native_pdf_over_converted(self):
        """Office 转换件场景：无原生 PDF 时回退到 converted/ 下的转换产物。"""
        sha = self._upload_pdf(pages=1)
        # 把上传件挪进 converted 模拟"Office 转换件已生成、原件是非 PDF"的布局
        converted = self.settings.upload_dir / "converted"
        converted.mkdir(parents=True, exist_ok=True)
        (self.settings.upload_dir / f"{sha}.pdf").rename(converted / f"{sha}.pdf")
        page1 = self.client.get(f"/api/v1/files/{sha}/page/1")
        self.assertEqual(page1.status_code, 200)

    def test_page_image_rejects_bad_requests(self):
        self.assertEqual(self.client.get("/api/v1/files/not-a-sha/page/1").status_code, 400)
        self.assertEqual(self.client.get(f"/api/v1/files/{'0'*64}/page/0").status_code, 400)
        self.assertEqual(self.client.get(f"/api/v1/files/{'0'*64}/page/1").status_code, 404)

    def test_batch_list_endpoint(self):
        """GET /batches：批次下拉数据源，逐批带概览计数。"""
        upload = self.client.post("/api/v1/batches", files=[
            ("files", ("a.jpg", _png_bytes(), "image/jpeg")),
            ("files", ("b.jpg", _png_bytes(), "image/jpeg"))]).json()["batch_id"]
        listing = self.client.get("/api/v1/batches").json()
        self.assertEqual(listing[0]["id"], upload)
        self.assertEqual((listing[0]["total"], listing[0]["pending"]), (2, 2))

    def test_single_task_retry_semantics(self):
        """POST /tasks/{id}/retry：failed 可重排；done/永久失败拒绝。"""
        task_id = self.store.create_task(task_type="card", filename="x.jpg",
                                         file_sha256="s", file_path="/x/x.jpg")
        self.store.claim_next_task("w1")
        self.assertEqual(self.client.post(f"/api/v1/tasks/{task_id}/retry").status_code, 409)  # running 不可重试
        self.store.fail_task(task_id, error_code="WORKER_ERROR", error_message="boom")
        ok = self.client.post(f"/api/v1/tasks/{task_id}/retry")
        self.assertEqual((ok.status_code, ok.json()["status"]), (200, "queued"))
        self.store.claim_next_task("w2")
        self.store.finish_task(task_id, result_json="{}", engine_type="card_ocr")
        self.assertEqual(self.client.post(f"/api/v1/tasks/{task_id}/retry").status_code, 409)  # done 不可重试
        perm = self.store.create_task(task_type="card", filename="y.jpg",
                                      file_sha256="s2", file_path="/x/y.jpg")
        self.store.claim_next_task("w3")
        self.store.fail_task(perm, error_code="UNSUPPORTED_FORMAT", error_message="bad")
        self.assertEqual(self.client.post(f"/api/v1/tasks/{perm}/retry").status_code, 409)  # 永久失败
        self.assertEqual(self.client.post("/api/v1/tasks/999/retry").status_code, 404)

    def test_retype_creates_hint_task_and_replaces_batch_item(self):
        """指定类型重识别：对已落盘文件带 hint 新建任务，批次项改指新任务。"""
        response = self.client.post("/api/v1/batches", files=[
            ("files", ("c.jpg", _png_bytes(), "image/jpeg"))]).json()
        batch_id = response["batch_id"]
        task_id = self.store.get_batch(batch_id)["items"][0]["task_id"]
        bad = self.client.post(f"/api/v1/batches/{batch_id}/items/{task_id}/retype",
                               json={"type": "passport"})
        self.assertEqual(bad.status_code, 422)
        ok = self.client.post(f"/api/v1/batches/{batch_id}/items/{task_id}/retype",
                              json={"type": "license"})
        self.assertEqual(ok.status_code, 200)
        new_id = ok.json()["task_id"]
        first = self.store.claim_next_task("w1", claimable_types=["card"])   # 同秒创建，旧任务先领
        second = self.store.claim_next_task("w2", claimable_types=["card"])
        self.assertEqual((second.id, second.hint_type), (new_id, "license"))
        self.assertEqual(first.id, task_id)
        items = self.store.get_batch(batch_id)["items"]        # 批次仍是一项，指向新任务
        self.assertEqual([i["task_id"] for i in items], [new_id])
        outsider = self.store.create_task(task_type="card", filename="z.jpg",
                                          file_sha256="s9", file_path="/x/z.jpg")
        not_in_batch = self.client.post(
            f"/api/v1/batches/{batch_id}/items/{outsider}/retype", json={"type": "license"})
        self.assertEqual(not_in_batch.status_code, 404)


if __name__ == "__main__":
    unittest.main()
