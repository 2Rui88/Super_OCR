import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app import figures, pipeline, queue, retrieval
from app.agent_business.citations import Citations
from app.agent_business.tools_knowledge import read_figure, search_chunks
from app.config import settings
from app.db import connect, init_db


class TableFiguresTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        temp = Path(work.name)
        for name, value in (("db_path", temp / "kb.sqlite3"), ("crop_dir", temp)):
            patcher = patch.object(settings, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.pdf = temp / "document.pdf"
        self.pdf.touch()
        init_db()
        with connect() as db:
            db.execute("INSERT INTO documents (id, filename, sha256, fmt, status, created_at)"
                       " VALUES (1, '工程设计.doc', 'sha', '.doc', 'pending', 1)")
        title, region = [0.1, 0.1, 0.8, 0.2], [0.1, 0.2, 0.9, 0.8]
        self.job = queue.claim_next("test-worker")
        pipeline.pack_chunks(1, self.job.lease_token, "工程设计.doc", {"pages": [
            {"page_num": 47, "source_mode": "ocr", "blocks": [
                {"type": "title", "bbox": title, "text": "附录E 例外转呈设计流程图"},
                {"type": "table", "bbox": region,
                 "row_texts": ["例外转呈设计流程图: 岗位", "填写结果 (100)", "无可用节点文字"]},
            ]},
            {"page_num": 48, "source_mode": "ocr", "blocks": [
                {"type": "title", "bbox": title, "text": "附录F 例外转呈设计流程节点说明"},
                {"type": "table", "bbox": region, "row_texts": ["流程节点说明: 编号", "填写结果 (100)"]},
            ]},
        ]})
        self.graph = {"kind": "flowchart", "lanes": [
            {"role": "主设人", "nodes": [{"id": "n1", "printed_id": "100", "label": "填写结果"}]},
            {"role": "部长", "nodes": [{"id": "n2", "printed_id": "110", "label": "核实"}]},
            {"role": "总工", "nodes": [{"id": "n3", "printed_id": "120", "label": "批准"},
                                       {"id": "n4", "printed_id": "", "label": "结束"}]},
        ], "edges": [
            {"from": "n1", "to": "n2"}, {"from": "n2", "to": "n3"},
            {"from": "n3", "to": "n4", "condition": "通过"},
        ], "notes": ["注：材料一并归档。"]}

    def get_page(self, page):
        with connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM chunks WHERE doc_id=1 AND page_num=? ORDER BY id", (page,),
            )]

    @staticmethod
    def make_crop(pdf_path, page_num, bbox, image, **kwargs):
        image.write_bytes(b"test crop")
        return image

    async def test_flowchart_enters_index_and_node_table_is_unchanged(self):
        page48 = self.get_page(48)
        raw = json.dumps(self.graph)
        with patch("app.llm.available", return_value=True), patch(
            "app.llm.vl_describe", new=AsyncMock(return_value=raw),
        ) as vision, patch.object(figures, "crop_figure", side_effect=self.make_crop) as cut:
            counts = await figures.process_doc_figures(1, self.job.lease_token, str(self.pdf))
            self.assertEqual(counts["recovered"], 1)
            self.assertFalse(await figures.recover_table_figures(1, self.job.lease_token, str(self.pdf), (47, 48)))
            vision.assert_awaited_once()
            self.assertEqual(cut.call_args.args[1], 47)
        self.assertEqual(page48, self.get_page(48))

        self.assertEqual(queue.finish(1, self.job.lease_token, ok=True), "ready")
        graph, = [row for row in self.get_page(47) if row["type"] == "figure"]
        self.assertTrue(Path(graph["crop_path"]).is_file())
        self.assertIn("100 主设人｜填写结果 → 110 部长｜核实", graph["seek_text"])
        self.assertIn("110 部长｜核实 → 120 总工｜批准", graph["vl_desc"])
        self.assertIn("120 总工｜批准 → 结束", graph["vl_desc"])
        self.assertIn("注：材料一并归档。", graph["seek_text"])
        self.assertNotIn("n2", graph["seek_text"])
        self.assertIn(graph["id"], retrieval._keyword_ids("材料一并归档", 1))
        self.assertIn(graph["id"], retrieval._keyword_ids("110 部长核实", 1))
        found = retrieval._keyword_ids("填写结果", 1)
        self.assertFalse(set(found) & {r["id"] for r in self.get_page(47) if r["type"].startswith("table")})
        self.assertTrue(set(found) & {r["id"] for r in page48 if r["type"].startswith("table")})
        with patch("app.llm.available", return_value=False):
            searched = await search_chunks({"query": "110 部长核实"}, Citations())
            question = "附录 E 流程图的图注是什么？"
            with patch.object(retrieval, "_keyword_ids", return_value=[]):
                direct = await retrieval.search(question)
                rephrased = await search_chunks({"query": "查阅补充说明"}, Citations(question))
        self.assertFalse(direct["refuse"])
        self.assertEqual(direct["results"][0]["id"], graph["id"])
        self.assertEqual(rephrased["blocks"][0]["chunk_id"], graph["id"])
        chart, = [block for block in searched["blocks"] if block["chunk_id"] == graph["id"]]
        self.assertIn("→ 120", chart["structure"])
        read = await read_figure({"chunk_id": graph["id"]}, Citations())
        self.assertIn("→ 结束", read["blocks"][0]["structure"])
        self.assertIn("注：材料一并归档。", read["blocks"][0]["structure"])
        self.assertEqual(read["blocks"][0]["image_url"], f"/api/v1/crops/{graph['id']}")

    async def test_table_uncertainty_and_dangling_arrow_preserve_table(self):
        self.graph["edges"][0]["to"] = "missing"
        before = self.get_page(47)
        cases = [json.dumps({"kind": "table"}), json.dumps({"kind": "uncertain"}), json.dumps(self.graph)]
        with patch("app.llm.available", return_value=True), patch(
            "app.llm.vl_describe", new=AsyncMock(side_effect=cases),
        ), patch.object(figures, "crop_figure", side_effect=self.make_crop):
            for _ in cases:
                self.assertEqual(await figures.recover_table_figures(1, self.job.lease_token, str(self.pdf)), [])
        self.assertEqual(before, self.get_page(47))
