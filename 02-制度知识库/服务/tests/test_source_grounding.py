import json
import tempfile
import unittest
from contextlib import aclosing
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.agent_business.articles import label_articles, select_clause
from app.agent_business.citations import Citations
from app.agent_business.document_scope import preferred_document, requested_document, resolve_document
from app.agent_business.monetary_answer import calculation_stream, checked_reference_line, render_amounts
from app.agent_business.policy_calculations import policy_answer
from app.agent_business.policy_editions import current_editions
from app.agent_business.tools_knowledge import _block, read_table, search_chunks
from app.config import settings
from app.db import connect, init_db


class SourceGroundingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        db_path = patch.object(settings, "db_path", Path(temp.name) / "kb.sqlite3")
        db_path.start()
        self.addCleanup(db_path.stop)
        init_db()
        with connect() as db:
            db.executemany(
                "INSERT INTO documents(id,filename,fmt,sha256,status,created_at) "
                "VALUES(?,?,'pdf',?,'ready',0)",
                [(1, "印章管理规定2021（扫描）.pdf", "seal-old"),
                 (2, "印章使用管理规定（2023）.pdf", "seal-new"),
                 (3, "差旅费管理办法2021.pdf", "travel"),
                 (4, "差旅费管理办法2021.pdf", "travel")],
            )

    def add_chunk(self, doc_id, page, body, *, heading="", kind="text", bbox=None, html=None):
        with connect() as db:
            cursor = db.execute(
                "INSERT INTO chunks(doc_id,page_num,heading_path,type,bbox,seek_text,html,created_at)"
                " VALUES(?,?,?,?,?,?,?,0)",
                (doc_id, page, heading, kind, json.dumps(bbox or [0, 0, 1, 1]),
                 f"制度文件.pdf\n{body}", html),
            )
            return dict(db.execute(
                "SELECT c.*, d.filename FROM chunks c JOIN documents d ON d.id=c.doc_id WHERE c.id=?",
                (cursor.lastrowid,),
            ).fetchone())

    def test_explicit_version_overrides_misresolved_alias(self):
        self.add_chunk(1, 1, "发文", heading="印章使用管理规定（2021）")
        self.assertEqual(resolve_document(
            2, "印章使用管理规定", "按2021年盖章扫描件《印章使用管理规定》，加急送谁审批？"
        ), (1, ""))
        self.assertEqual(resolve_document(
            None, "印章使用管理规定", "按《印章使用管理规定（2023）》，合同章怎么审批？"
        ), (2, ""))

    def test_equivalent_names_are_canonical_and_different_copies_stay_ambiguous(self):
        self.assertEqual(resolve_document(None, "差旅费管理办法2021.pdf"), (4, ""))
        with connect() as db:
            db.execute("UPDATE documents SET sha256='another' WHERE id=4")
        selected, message = resolve_document(None, "差旅费管理办法2021.pdf")
        self.assertIsNone(selected)
        self.assertIn("多份不同制度", message)
        self.assertIn("ID 3", message)
        self.assertIn("ID 4", message)

    async def test_quoted_topics_find_evidence_and_explicit_scope_stays_locked(self):
        body = self.add_chunk(2, 5, "《用印管理细则》由总裁办公室归口。")
        question = "印章管理规定里提到的《用印管理细则》由哪个部门归口？"
        self.assertEqual(requested_document(question), (None, ""))
        self.assertIsNone(preferred_document(question))
        with patch("app.retrieval.search", new=AsyncMock(return_value={"results": []})) as search:
            result = await search_chunks({"query": "文号 归口"}, Citations(question))
        search.assert_awaited_once_with("文号 归口", doc_id=None, top_k=6)
        self.assertEqual(result["blocks"][0]["chunk_id"], body["id"])
        explicit = "只查《印章使用管理规定（2023）》，《用印管理细则》由谁管理？"
        self.assertEqual(requested_document(explicit), (2, ""))
        with patch("app.retrieval.search", new=AsyncMock(return_value={"results": []})) as search:
            await search_chunks({"query": "文号 归口"}, Citations(explicit))
        search.assert_awaited_once_with("文号 归口", doc_id=2, top_k=6)

    def test_current_edition_requires_an_effective_clause_and_keeps_historical_requests(self):
        older = self.add_chunk(1, 2, "总裁办公室每年检查一次。")
        self.assertEqual(current_editions([older], "多久检查一次？"), [])
        self.add_chunk(2, 7, "本规定自2023年5月15日起施行。")
        self.assertEqual(current_editions([older], "多久检查一次？"), [2])
        self.assertEqual(current_editions([older], "按2021年规定多久检查一次？"), [])

    async def test_appendix_node_numbers_match_the_rows_in_that_appendix(self):
        first = self.add_chunk(2, 5, "节点说明: 100 | 2: 审核 | 3: 项目管理部", heading="附录B", kind="table_row")
        second = self.add_chunk(2, 5, "节点说明: 110 | 2: 审核 | 3: 经营部", heading="附录B", kind="table_row")
        self.add_chunk(2, 6, "节点说明: 110 | 2: 审核 | 3: 其他部门", heading="附录D", kind="table_row")
        with patch("app.retrieval.search", new=AsyncMock(return_value={"results": []})):
            result = await search_chunks({"query": "审核"}, Citations("附录B的节点100和110由谁审核？"))
        self.assertEqual([block["chunk_id"] for block in result["blocks"]], [first["id"], second["id"]])

    async def test_amount_continuation_removes_footer_and_cites_its_own_page(self):
        begin = self.add_chunk(2, 6, "第十五条交通费标准为每天6", heading="第五章交通", bbox=[0.1, 0.6, 0.9, 0.95])
        end = self.add_chunk(2, 7, "100元。接待方提供交通工具时不再发放。", heading="第五章交通",
                             bbox=[0.1, 0.12, 0.9, 0.2])
        with patch("app.retrieval.search", new=AsyncMock(return_value={"results": [end, begin]})):
            result = await search_chunks({"query": "交通费每天多少"}, Citations("交通费每天多少？"))
        self.assertEqual([block["chunk_id"] for block in result["blocks"]], [begin["id"], end["id"]])
        self.assertNotIn("每天6", result["blocks"][0]["text"])
        self.assertIn("100元", result["blocks"][1]["text"])
        self.assertIn("第7页 · 第十五条", result["blocks"][1]["label"])

    def test_evidence_uses_the_named_html_row_and_structural_article(self):
        table = self.add_chunk(
            2, 5, "质量技术部: 信息中心 | 甲: 乙 | 内部审核: OA电子签章", kind="table_row",
            html="<table><tr><td>质量技术部</td><td>甲</td><td>内部审核</td></tr>"
                 "<tr><td>信息中心</td><td>乙</td><td>OA电子签章</td></tr></table>",
        )
        article = self.add_chunk(2, 3, "第三章用印审批第七条出图章申请。其他印章按本章第九条执行。",
                                 heading="第三章用印审批")
        self.assertEqual(_block(table, Citations("信息中心负责什么？"))["text"], "信息中心 | 乙 | OA电子签章")
        self.assertEqual(_block(article, Citations("出图章怎么申请？"))["article"], "第七条")

    def test_amounts_are_calculated_once_and_require_valid_references(self):
        answer = json.dumps({"items": [
            {"label": "住宿费", "rate": 450, "count": 2, "deductions": [], "refs": [1], "basis": "一类城市"},
            {"label": "伙食补助", "rate": 100, "count": 3, "deductions": [{"rate": 30, "count": 2}],
             "refs": [2], "basis": "统一安排两餐"},
            {"label": "市内交通费", "rate": 100, "count": 3, "deductions": [], "refs": [3], "basis": ""},
        ]})
        rendered = render_amounts(answer, {1, 2, 3})
        self.assertIn("1440 元", rendered)
        self.assertIn("100 × 3 − 30 × 2 = 240 元", rendered)
        with self.assertRaises(ValueError):
            render_amounts(answer, {1, 2})

    def test_design_change_either_threshold_and_signer_are_taken_from_evidence(self):
        table = {"n": 1, "source_id": 3, "policy_table": [
            ["电压等级 类型", "220kV", "110kV及35kV"],
            ["重大设计变更", "变更金额≥200万元或变更金额与单项工程概算比率≥5%",
             "变更金额≥100万元或变更金额与单项工程概算比率≥5%"],
        ]}
        signer = {"n": 2, "source_id": 3,
                  "text": "一般设计变更由副总工程师签署批准；重大设计变更由总工程师签署批准。"}
        by_amount = policy_answer("220kV工程设计变更300万元，占3%，属于哪一类，由谁签署批准？", [table, signer])
        self.assertIn("总工程师**签署批准 [2]", by_amount)
        by_share = policy_answer("110kV工程设计变更25万元，占6%，属于哪一类？", [table])
        self.assertIn("占概算比例 6% 已达到 5%", by_share)
        self.assertEqual(policy_answer("110kV工程设计变更25万元，占3%，属于哪一类？", [table]), "")

    def test_trip_total_uses_the_city_list_and_columns_from_one_source(self):
        paragraphs = [
            {"n": 1, "source_id": 3, "text": "一类城市：杭州、成都、武汉；二类城市：其他省会。"},
            {"n": 2, "source_id": 3, "policy_table": [
                ["320", "380", "450", "中级及以下"], ["三类城市", "二类城市", "一类城市", "职级"],
            ]},
            {"n": 3, "source_id": 3, "text": "伙食补助标准为每人每天100元，按每餐30元扣减。"},
            {"n": 4, "source_id": 3, "text": "【第十五条续】100元。接待单位提供交通工具时不发放市内交通费。"},
        ]
        question = "中级职称去成都出差3个自然日，住2晚按上限，安排了2顿饭，接待单位没有提供交通工具。合计多少？"
        self.assertIn("1440 元", policy_answer(question, paragraphs))
        self.assertEqual(policy_answer(question, paragraphs[:3] + [{**paragraphs[3], "source_id": 4}]), "")

    def test_mileage_calculation_requires_approval_and_both_exclusions(self):
        evidence = [
            {"n": 1, "source_id": 3, "text": "按每公里0.8元补助，高速通行费、停车费凭票报销，不再报销同程城市间交通费。"},
            {"n": 2, "source_id": 3, "text": "出差人员自驾的，不再发放市内交通费。"},
        ]
        self.assertIn("240 元", policy_answer("经批准自驾出差300公里，如何报销？", evidence))
        self.assertEqual(policy_answer("未经批准自驾出差300公里，如何报销？", evidence), "")

    def test_unavailable_reference_uses_the_unique_verified_article(self):
        self.assertEqual(checked_reference_line("依据第七条 [7]。", {4, 5}, {"第七条": {5}}), "依据第七条 [5]。")
        self.assertEqual(checked_reference_line("依据第七条 [7]。", {4, 5}, {"第七条": {4, 5}}), "依据第七条 。")

    async def test_closing_answer_also_closes_model_stream(self):
        closed = []

        async def upstream(_messages, *, tools):
            try:
                yield "没有找到相关规定"
                yield "后续内容"
            finally:
                closed.append(True)

        adapter = calculation_stream("试用期多久？", Citations())
        messages = [{"role": "user", "content": "资料：（无）"}]
        with patch("app.llm.chat_stream", upstream):
            async with aclosing(adapter(messages, tools=[])) as stream:
                self.assertEqual(await anext(stream), "没有找到相关规定")
        self.assertEqual(closed, [True])

    def test_diagram_step_and_legend_use_the_same_native_page(self):
        chunk = self.add_chunk(1, 13, "注：图示说明：△起草，审查，□会签。")
        question = "业务流程图里，△和□是什么？步骤50做什么？"
        with patch("app.agent_business.tools_knowledge._native_lines",
                   return_value=(["50", "相关部门会签", "注：图示说明：△起草，○审查，□会签。"], "")):
            block = _block(chunk, Citations(question))
        self.assertIn("表示起草", policy_answer(question, [block]))
        self.assertIn("步骤 50 是**相关部门会签** [1]", policy_answer(question, [block]))

    def report_chunks(self):
        with connect() as db:
            db.execute(
                "INSERT INTO documents(id,filename,fmt,sha256,status,created_at) "
                "VALUES(5,'工程设计（咨询）过程控制程序.doc','doc','design','ready',0)"
            )
        toc = self.add_chunk(5, 1, "4.10设计回访........11\n4.11设计文件记录归档......12", heading="目录")
        section = self.add_chunk(
            5, 22, "4.10设计回访\n中型工程的工程回访报告在15天内完成，大型工程的工程回访报告在1个月内完成。",
            heading="4.10设计回访",
        )
        continuation = self.add_chunk(5, 23, "a）设计回访综述。", heading="4.10设计回访")
        return toc, section, continuation

    @staticmethod
    def native_report_lines(chunk):
        return {
            1: (["4.10 设计回访........11"], ""),
            22: (["4.10 设计回访", "中型工程的工程回访报告在15天内完成"], "20"),
            23: (["a）设计回访综述。"], "21"),
        }[chunk["page_num"]]

    def test_report_location_selects_requested_page_and_its_own_citation(self):
        toc, section, continuation = self.report_chunks()
        cases = (
            ("“设计回访”在第几页？回访报告要在多长时间内完成？", "原 Word 目录", True),
            ("Word目录上，设计回访标在哪一页？", "原 Word 目录", False),
            ("目录是11页，设计回访在PDF阅读器里要翻到第几页？", "在服务器 PDF 中", False),
            ("服务器PDF中设计回访的页脚印着什么？", "服务器 PDF 的**页脚标为 20**", False),
        )
        for question, first, asks_deadline in cases:
            with self.subTest(question=question), patch(
                "app.agent_business.tools_knowledge._native_lines", side_effect=self.native_report_lines,
            ):
                citations = Citations(question)
                evidence = [_block(chunk, citations) for chunk in (continuation, section, toc)]
                answer = policy_answer(question, evidence)
                self.assertTrue(answer.startswith(first), answer)
                self.assertIn("**第 11 页** [3]", answer)
                self.assertIn("PDF 第 22 页", answer)
                self.assertIn("页脚标为 20", answer)
                self.assertNotIn("PDF 第 23 页", answer)
                self.assertEqual("**15天**" in answer and "**1个月**" in answer, asks_deadline)
                unrelated = {**evidence[-1], "source_id": 3}
                self.assertNotIn("第 11 页", policy_answer(question, [evidence[1], unrelated]))

    async def test_report_search_recovers_section_start_and_toc_from_same_document(self):
        toc, section, continuation = self.report_chunks()
        question = "《工程设计（咨询）过程控制程序》的设计回访，在 PDF 里要翻到第几页？"
        with patch("app.retrieval.search", new=AsyncMock(return_value={"results": [continuation]})), patch(
            "app.agent_business.tools_knowledge._native_lines", side_effect=self.native_report_lines,
        ):
            result = await search_chunks({"query": "设计回访 PDF 页码", "top_k": 1}, Citations(question))
        self.assertEqual({block["chunk_id"] for block in result["blocks"]},
                         {toc["id"], section["id"], continuation["id"]})
        self.assertTrue(policy_answer(question, result["blocks"]).startswith(
            "在服务器 PDF 中，“设计回访”位于 **PDF 第 22 页**"))

    def test_seal_procedure_keeps_retention_period_and_the_controlling_article(self):
        evidence = [
            {"n": 3, "text": "第十五条用印完成后系统自动形成电子台账。纸质《用印登记簿》不再使用。"
                              "OA用印台账永久保存，用印文件扫描件保存期不少于10年。"},
            {"n": 5, "text": "第七条出图章、合同章用印：OA提交用印申请，部门负责人审批，合同章另需经营管理部审核；"
                              "加急当天可盖但须总裁办公室主任批准。其他印章按本章第九条执行。"},
        ]
        retention = policy_answer("还需要在纸质《用印登记簿》上登记吗？记录保存多久？", evidence)
        self.assertIn("不少于 10 年", retention)
        self.assertIn("第十五条 [3]", retention)
        urgent = policy_answer("出图专用章、合同专用章加急用印怎么处理？", evidence)
        self.assertIn("按第七条", urgent)
        self.assertNotIn("第九条", urgent)

    async def test_signature_evidence_is_ranked_by_question_topic(self):
        self.add_chunk(2, 2, "人事任命由分管领导签署批准。")
        signer = self.add_chunk(2, 3, "重大设计变更由总工程师签署批准。")
        batch = self.add_chunk(2, 4, "设计变更的批复由建设单位作出。")
        with patch("app.retrieval.search", new=AsyncMock(return_value={"results": [batch]})):
            result = await search_chunks({"query": "设计变更批准"}, Citations("重大设计变更由谁签署批准？"))
        self.assertEqual(result["blocks"][0]["chunk_id"], signer["id"])

    async def test_table_header_and_data_are_returned_with_their_own_pages(self):
        header = self.add_chunk(
            4, 2, "差旅住宿费", heading="第四章 住宿费", kind="table_full",
            bbox=[0.12, 0.87, 0.88, 0.91],
            html="<table><tr><td>职级</td><td>一类城市</td><td>三类城市</td></tr></table>",
        )
        body = self.add_chunk(
            4, 3, "正高级住宿费", heading="第四章 住宿费", kind="table_full",
            bbox=[0.12, 0.08, 0.88, 0.19],
            html="<table><tr><td>正高级</td><td>520</td><td>380</td></tr></table>",
        )
        self.add_chunk(
            4, 3, "无关表", heading="第五章", kind="table_full",
            bbox=[0.12, 0.08, 0.88, 0.19],
            html="<table><tr><td>其他</td><td>1</td><td>2</td></tr></table>",
        )
        result = await read_table({"chunk_id": header["id"]}, Citations())
        self.assertEqual([block["chunk_id"] for block in result["blocks"]], [header["id"], body["id"]])
        self.assertEqual(result["blocks"][1]["header"], ["职级", "一类城市", "三类城市"])
        self.assertEqual(result["blocks"][1]["rows"], [["正高级", "520", "380"]])
        self.assertIn("第3页", result["blocks"][1]["label"])

    def test_article_continues_until_next_explicit_heading(self):
        self.add_chunk(2, 1, "第三条适用范围。第四条遵循下列原则：（一）依法决策。", heading="第一节")
        chunk = self.add_chunk(
            2, 2, "承接前页。（三）坚持集体决策。\n第五条权责清单另行印发。", heading="第一节",
        )
        actual = label_articles(chunk, chunk["seek_text"].partition("\n")[2])
        self.assertTrue(actual.startswith("【第四条续】"))
        self.assertIn("（三）坚持集体决策。\n【新条款】第五条", actual)
        excerpt, article = select_clause(actual, ["集体决", "体决策"])
        self.assertEqual(article, "第四条第（三）项")
        self.assertNotIn("第五条", excerpt)

    def test_excerpt_keeps_relevant_article_crossing_preview_boundary(self):
        article = "第十六条 加急用印应电话请示分管领导，并于次日补办书面审批。"
        chunk = self.add_chunk(1, 3, "无关业务。" * 76 + "\n" + article + ("其他登记。" * 10))
        query = "加急用印应请示谁？何时补办书面审批？"
        self.assertIn(article, _block(chunk, Citations(query))["text"])

    async def test_flow_question_keeps_original_query_figure(self):
        table = self.add_chunk(2, 48, "节点说明", kind="table_full")
        figure = self.add_chunk(2, 47, "例外转呈箭头", kind="figure", heading="例外转呈流程图")
        figure["vl_desc"] = "100 → 110 → 120"
        original = "例外转呈流程填写处置结果后哪些节点才结束？"
        rewritten = "处置结果 节点说明"
        with patch("app.retrieval.search", new=AsyncMock(side_effect=[
            {"results": [table]}, {"results": [table, figure]},
        ])) as search:
            result = await search_chunks({"query": rewritten}, Citations(original))
        self.assertEqual([call.args[0] for call in search.await_args_list], [rewritten, original])
        self.assertEqual(result["blocks"][0]["chunk_id"], figure["id"])
        self.assertIn("110", result["blocks"][0]["structure"])
