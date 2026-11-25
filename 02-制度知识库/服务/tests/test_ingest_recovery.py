"""离线回归：SQLite 任务恢复、OCR 提交幂等和独立租约。"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db, llm, p1client, pipeline, queue, vector
from app.api.app import app
from app.config import settings


RESULT = {
    "pages": [{
        "page_num": 1, "source_mode": "ocr",
        "blocks": [{"type": "text", "text": "文件正文"}],
    }],
}


def _done() -> httpx.Response:
    return httpx.Response(200, json={"status": "done", "result": {"result": RESULT}})


class IngestRecoveryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.multiple(
            settings, db_path=root / "kb.sqlite3", data_dir=root, max_attempts=3,
            drain_timeout_seconds=0.01, p1_base_url="http://mock-ocr/api/v1",
            p1_resubmit_max=2,
        ))
        self.enterContext(patch.object(llm, "available", return_value=False))
        self.pdf = root / "document.pdf"
        with pymupdf.open() as pdf:
            pdf.new_page()
            pdf.save(self.pdf)
        db.init_db()
        _, self.doc_id = queue.enqueue("document.pdf", ".pdf", "sha", pdf_path=str(self.pdf))

    def _mock_ocr(self, handler):
        client = httpx.AsyncClient
        self.enterContext(patch.object(
            p1client.httpx, "AsyncClient",
            side_effect=lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs),
        ))

    def _row(self):
        with db.connect() as conn:
            return conn.execute("SELECT * FROM documents WHERE id=?", (self.doc_id,)).fetchone()

    def _claim(self):
        job = queue.claim_next("host:pid")
        self.assertIsNotNone(job)
        return job

    async def test_service_restart_polls_the_same_task(self):
        requests = []
        first_poll = asyncio.Event()

        async def ocr(request):
            requests.append((request.method, request.url.path))
            if request.method == "POST":
                return httpx.Response(202, json={"task_id": 31})
            if not first_poll.is_set():
                first_poll.set()
                await asyncio.Event().wait()
            return _done()

        self._mock_ocr(ocr)
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(first_poll.wait(), timeout=5)
            before = self._row()
            self.assertEqual(before["p1_task_id"], 31)
            self.assertTrue(before["p1_request_key"])
        self.assertEqual(self._row()["status"], "pending")

        async with app.router.lifespan_context(app):
            async with asyncio.timeout(5):
                while self._row()["status"] != "ready":
                    await asyncio.sleep(0.01)
        after = self._row()
        self.assertEqual(after["attempts"], 1)
        self.assertEqual(after["p1_task_id"], 31)
        self.assertEqual(json.loads(after["p1_result_json"]), RESULT)
        self.assertEqual(requests, [
            ("POST", "/api/v1/documents"), ("GET", "/api/v1/tasks/31"),
            ("GET", "/api/v1/tasks/31"),
        ])

    async def test_post_response_lost_reuses_persisted_idempotency_key(self):
        submitted = {}
        keys = []

        def ocr(request):
            if request.method == "POST":
                key = request.headers["Idempotency-Key"]
                self.assertEqual(self._row()["p1_request_key"], key)
                task_id = submitted.setdefault(key, len(submitted) + 1)
                keys.append(key)
                if len(keys) == 1:
                    raise httpx.ReadTimeout("202 未送达", request=request)
                return httpx.Response(202, json={"task_id": task_id})
            self.assertEqual(request.url.path, "/api/v1/tasks/1")
            return _done()

        self._mock_ocr(ocr)
        with self.assertLogs("kb.ingest", level="ERROR"):
            self.assertFalse((await pipeline.run_ingest(self._claim()))[0])
        self.assertIsNone(self._row()["p1_task_id"])
        self.assertEqual(await pipeline.run_ingest(self._claim()), (True, ""))
        self.assertEqual(keys, [keys[0], keys[0]])
        self.assertEqual(len(submitted), 1)

    async def test_poll_network_error_keeps_the_task_id(self):
        requests = []

        def ocr(request):
            requests.append((request.method, request.url.path))
            if request.method == "POST":
                return httpx.Response(202, json={"task_id": 31})
            if len(requests) == 2:
                raise httpx.ReadTimeout("查询超时", request=request)
            return _done()

        self._mock_ocr(ocr)
        with self.assertLogs("kb.ingest", level="ERROR"):
            self.assertFalse((await pipeline.run_ingest(self._claim()))[0])
        self.assertEqual(self._row()["p1_task_id"], 31)
        self.assertEqual(await pipeline.run_ingest(self._claim()), (True, ""))
        self.assertEqual(requests, [
            ("POST", "/api/v1/documents"), ("GET", "/api/v1/tasks/31"),
            ("GET", "/api/v1/tasks/31"),
        ])

    async def test_confirmed_worker_loss_creates_a_new_task(self):
        keys = []

        def ocr(request):
            if request.method == "POST":
                keys.append(request.headers["Idempotency-Key"])
                return httpx.Response(202, json={"task_id": len(keys)})
            if request.url.path.endswith("/1"):
                return httpx.Response(200, json={"status": "failed", "error_code": "WORKER_LOST"})
            self.assertEqual(request.url.path, "/api/v1/tasks/2")
            return _done()

        self._mock_ocr(ocr)
        with self.assertLogs("kb.p1", level="WARNING"):
            self.assertEqual(await pipeline.run_ingest(self._claim()), (True, ""))
        self.assertEqual(len(set(keys)), 2)
        self.assertEqual((self._row()["p1_task_id"], self._row()["p1_request_key"]), (2, keys[1]))

    async def test_ocr_result_survives_a_failure_during_chunking(self):
        requests = []

        def ocr(request):
            requests.append(request.method)
            return httpx.Response(202, json={"task_id": 31}) if request.method == "POST" else _done()

        self._mock_ocr(ocr)
        with patch.object(pipeline, "pack_chunks", side_effect=RuntimeError("切块失败")):
            with self.assertLogs("kb.ingest", level="ERROR"):
                self.assertFalse((await pipeline.run_ingest(self._claim()))[0])
        self.assertEqual(json.loads(self._row()["p1_result_json"]), RESULT)
        self.assertEqual(await pipeline.run_ingest(self._claim()), (True, ""))
        self.assertEqual(requests, ["POST", "GET"])

    async def test_stale_claim_with_checkpoint_resumes_at_the_attempt_limit(self):
        job = self._claim()
        queue.save_submission(job.id, job.lease_token, str(self.pdf), "submission", 31)
        with db.connect() as conn:
            conn.execute("UPDATE documents SET heartbeat_at=0, attempts=3 WHERE id=?", (job.id,))
        self.assertEqual(queue.reclaim_stale(), 1)
        resumed = self._claim()
        self.assertNotEqual(resumed.lease_token, job.lease_token)
        self.assertEqual((resumed.p1_task_id, resumed.p1_request_key), (31, "submission"))
        self.assertEqual(self._row()["attempts"], 3)

    async def test_reparse_fences_old_claim_from_the_same_process(self):
        old = self._claim()
        self.assertTrue(queue.reset_for_reparse(old.id))
        new = self._claim()
        self.assertEqual(old.worker, new.worker)
        self.assertNotEqual(old.lease_token, new.lease_token)
        self.assertEqual(queue.renew_lease(old.id, old.lease_token), queue.LEASE_LOST)
        self.assertEqual(queue.renew_lease(new.id, new.lease_token), queue.LEASE_RENEWED)
        self.assertFalse(queue.holds_lease(old.id, old.lease_token))
        self.assertIsNone(queue.finish(old.id, old.lease_token, ok=True))

        with self.assertRaises(queue.LeaseLost):
            queue.set_phase(old.id, old.lease_token, "旧进度")
        with self.assertRaises(queue.LeaseLost):
            queue.save_submission(old.id, old.lease_token, str(self.pdf), "old", 1)
        with self.assertRaises(queue.LeaseLost):
            queue.save_ocr_result(old.id, old.lease_token, "{}")
        await asyncio.to_thread(pipeline.pack_chunks, new.id, new.lease_token, new.filename, RESULT)
        with self.assertRaises(queue.LeaseLost):
            await asyncio.to_thread(pipeline.pack_chunks, old.id, old.lease_token, old.filename, {})
        with self.assertRaises(queue.LeaseLost):
            await vector.remove_doc(old.id, lease_token=old.lease_token)
        with self.assertRaises(queue.LeaseLost):
            await vector.add_chunks(old.id, old.lease_token)
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0], 1)
        self.assertEqual(self._row()["status"], "processing")

    async def test_replaced_conversion_cannot_publish_its_pdf(self):
        source = self.pdf.with_suffix(".docx")
        source.write_bytes(b"word")
        old = self._claim()

        def convert(args):
            output_dir = Path(args[args.index("--outdir") + 1])
            (output_dir / source.with_suffix(".pdf").name).write_bytes(self.pdf.read_bytes())
            queue.reset_for_reparse(old.id)
            return subprocess.CompletedProcess(args, 0)

        with patch.object(pipeline, "_find_soffice", return_value="soffice"), \
             patch.object(pipeline, "_run_cmd", side_effect=convert):
            with self.assertRaises(queue.LeaseLost):
                await asyncio.to_thread(pipeline.convert_to_pdf, source, old)
        self.assertFalse((settings.data_dir / "converted" / f"doc{old.id}.pdf").exists())

    async def test_legacy_schema_gains_checkpoint_and_lease_columns(self):
        legacy_db = self.pdf.with_name("legacy.sqlite3")
        new_columns = ("lease_token", "p1_request_key", "p1_task_id")
        schema = "\n".join(line for line in db._SCHEMA.splitlines()
                           if not line.lstrip().startswith(new_columns))
        with db.connect(legacy_db) as conn:
            conn.executescript(schema)
            conn.execute(
                "INSERT INTO documents (filename, fmt, sha256, created_at) VALUES (?,?,?,?)",
                ("legacy.pdf", ".pdf", "legacy", 1))
        with patch.object(settings, "db_path", legacy_db):
            db.init_db()
            db.init_db()
            job = self._claim()
            self.assertEqual(job.filename, "legacy.pdf")
            self.assertTrue(job.lease_token)
            self.assertIsNone(job.p1_task_id)
            self.assertIsNone(job.p1_request_key)


if __name__ == "__main__":
    unittest.main()
