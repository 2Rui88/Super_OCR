import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pymupdf

from app import llm, pipeline, queue, vector
from app.api.app import app
from app.config import settings
from app.db import connect, init_db


class DocumentImportTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        resources = ExitStack()
        self.addCleanup(resources.close)
        self.root = Path(resources.enter_context(tempfile.TemporaryDirectory()))
        resources.enter_context(patch.multiple(
            settings, data_dir=self.root, db_path=self.root / "kb.sqlite3",
            upload_dir=self.root, crop_dir=self.root, embed_dim=2,
        ))
        resources.enter_context(patch.object(vector, "_index", None))
        init_db()

    async def test_same_file_uploads_run_as_independent_documents(self):
        with pymupdf.open() as document:
            document.new_page()
            pdf = document.tobytes()
        ocr = {"pages": [{"page_num": 1, "source_mode": "ocr", "blocks": [
            {"type": "text", "bbox": [0, 0, 1, 1], "text": "独立入库验证正文"},
        ]}]}
        file = ("files", ("验证.pdf", pdf, "application/pdf"))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            response = await client.post("/api/v1/documents", files=[file, file])
            response.raise_for_status()
            first = response.json()["results"]
            self.assertEqual([item["outcome"] for item in first], ["queued", "queued"])
            doc_ids = [item["doc_id"] for item in first]

            with (patch.object(pipeline, "recognize_pdf", AsyncMock(return_value=ocr)) as recognize,
                  patch.object(llm, "available", return_value=True),
                  patch.object(llm, "embed", AsyncMock(return_value=[[1.0, 0.0]])) as embed):
                job = queue.claim_next("test")
                self.assertEqual(job.id, doc_ids[0])
                self.assertTrue((await pipeline.run_ingest(job))[0])

                response = await client.post("/api/v1/documents", files=[file])
                response.raise_for_status()
                third, = response.json()["results"]
                self.assertEqual(third["outcome"], "queued")
                doc_ids.append(third["doc_id"])
                self.assertEqual(len(set(doc_ids)), 3)

                for doc_id in doc_ids[1:]:
                    job = queue.claim_next("test")
                    self.assertEqual(job.id, doc_id)
                    self.assertTrue((await pipeline.run_ingest(job))[0])
                self.assertEqual(recognize.await_count, 3)
                self.assertEqual(embed.await_count, 3)

            response = await client.get("/api/v1/documents")
            documents = response.json()["documents"]
            self.assertEqual({d["id"]: d["status"] for d in documents},
                             dict.fromkeys(doc_ids, "ready"))
        with connect() as db:
            chunks = {row["doc_id"]: row["id"] for row in db.execute("SELECT doc_id,id FROM chunks")}
        self.assertEqual(set(chunks), set(doc_ids))
        self.assertEqual({cid for cid, _ in await vector.search([1.0, 0.0])}, set(chunks.values()))

    def test_startup_clears_legacy_replacements(self):
        with connect() as db:
            db.executemany(
                "INSERT INTO documents (id,filename,fmt,sha256,status,created_at,"
                " superseded_by,replaces_target) VALUES (?,?,'.pdf',?,?,1,?,?)",
                [(2, "旧文件.pdf", "hash-2", "ready", 10, None),
                 (10, "新文件.pdf", "hash-10", "ready", None, None),
                 (11, "待处理文件.pdf", "hash-11", "pending", None, 2)],
            )
        init_db()
        init_db()
        with connect() as db:
            rows = db.execute("SELECT id,status,superseded_by,replaces_target FROM documents ORDER BY id")
            self.assertEqual([tuple(row) for row in rows],
                             [(2, "ready", None, None), (10, "ready", None, None),
                              (11, "pending", None, None)])

    def test_word_copies_have_independent_conversion_paths(self):
        source = self.root / "copy.docx"
        source.touch()

        def fake_conversion(_, output):
            output.write_bytes(b"converted")

        queue.enqueue(source.name, ".docx", "same-sha")
        queue.enqueue(source.name, ".docx", "same-sha")
        first_job = queue.claim_next("test")
        second_job = queue.claim_next("test")
        with (patch.object(pipeline, "_find_soffice", return_value=None),
              patch.object(pipeline, "_word_com_convert", side_effect=fake_conversion) as convert):
            first = pipeline.convert_to_pdf(source, first_job)
            second = pipeline.convert_to_pdf(source, second_job)
            self.assertEqual(pipeline.convert_to_pdf(source, first_job), first)
            self.assertEqual(convert.call_count, 2)
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), second.read_bytes())
