"""证件字段组装的几何回归(不加载 OCR 模型)。

覆盖 assemble_fields 的空间匹配规则:多行值吞行、水印行剔除、
相邻列标签作为吞行边界。原出自 test_optimizations.py(旧性能战役遗留
混合文件),拆分后仅保留与当前链路相关的组装回归。
"""
from __future__ import annotations

import sys
import io
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.certs import load_certs  # noqa: E402
from app.core.card_pipeline import _fuzzy_anchor_hit, assemble_fields, build_card_result  # noqa: E402
from app.core.image_routing import process_image  # noqa: E402
from app.engine.base import OcrLine  # noqa: E402
from app.engine.paddle_card import PaddleCardEngine  # noqa: E402


class MultilineAssemblyTest(unittest.TestCase):
    def test_multiline_field_does_not_reuse_the_name_box(self):
        cfg = load_certs(ROOT / "config/certs")["id_front"]
        lines = [
            OcrLine("住址", [0.05, 0.12, 0.12, 0.18]),
            OcrLine("姓名", [0.15, 0.12, 0.21, 0.18]),
            OcrLine("示例", [0.23, 0.12, 0.32, 0.18]),
        ]
        fields = {f.name: f.value for f in assemble_fields(lines, cfg)}
        self.assertEqual(fields["姓名"], "示例")
        self.assertEqual(fields["住址"], "")
    def test_multiline_value_ignores_left_label_and_overlapping_watermark(self):
        cfg = load_certs(ROOT / "config/certs")["id_front"]
        lines = [OcrLine("住址", [0.01, 0.205, 0.09, 0.245]),
                 OcrLine("第一行", [0.15, 0.20, 0.60, 0.25]),
                 OcrLine("第二行", [0.15, 0.27, 0.45, 0.32]),
                 OcrLine("样本", [0.40, 0.15, 0.70, 0.30]),
                 OcrLine("公民身份号码", [0.01, 0.40, 0.30, 0.45])]
        fields = {f.name: f for f in assemble_fields(lines, cfg)}
        self.assertEqual(fields["住址"].value, "第一行第二行")

    def test_multiline_stops_at_label_in_adjacent_column(self):
        cfg = load_certs(ROOT / "config/certs")["license"]
        lines = [OcrLine("经营范围", [0.01, 0.1, 0.2, 0.14]),
                 OcrLine("第一行", [0.3, 0.1, 0.8, 0.14]),
                 OcrLine("第二行", [0.3, 0.16, 0.8, 0.20]),
                 OcrLine("注册资本", [0.01, 0.22, 0.2, 0.26]),
                 OcrLine("伍佰万圆整", [0.3, 0.22, 0.6, 0.26]),
                 OcrLine("无关后续文本", [0.3, 0.28, 0.8, 0.32])]
        fields = {f.name: f for f in assemble_fields(lines, cfg)}
        self.assertEqual(fields["经营范围"].value, "第一行第二行")
        self.assertEqual(fields["注册资本"].value, "伍佰万圆整")


class EmptyLineAssemblyTest(unittest.TestCase):
    """空白处误检的框（识别为空文本、置信度 0）不参与组装：
    不贡献文字，也不能把字段置信度拉到 0、把 bbox 撑宽。"""

    def test_empty_text_line_between_label_and_value_is_ignored(self):
        cfg = load_certs(ROOT / "config/certs")["id_front"]
        lines = [OcrLine("性别", [0.08, 0.28, 0.17, 0.35], 0.81),
                 OcrLine("", [0.19, 0.29, 0.27, 0.34], 0.0),
                 OcrLine("女", [0.27, 0.28, 0.32, 0.35], 0.9999)]
        fields = {f.name: f for f in assemble_fields(lines, cfg)}
        self.assertEqual(fields["性别"].value, "女")
        self.assertEqual(fields["性别"].confidence, 0.9999)
        self.assertEqual(fields["性别"].bbox, [0.27, 0.28, 0.32, 0.35])


class BatchFieldExtractionTest(unittest.TestCase):
    def setUp(self):
        self.registry = load_certs(ROOT / "config/certs")

    def result(self, doc_type, lines):
        return build_card_result("sample.jpg", lines, self.registry,
                                 sha256="0" * 64, hint_type=doc_type)

    def test_credit_code_keeps_invalid_text_and_ocr_evidence(self):
        region = [0.06, 0.18, 0.71, 0.21]
        value_box = [0.35, 0.18, 0.70, 0.21]
        prefix = "统一社会信用代码 "
        cases = (
            ("919999O1MA8C2K7Q5X", "含非法字符 O"),
            ("91999901MA8C2K7Q50", "校验位错误，期望 X，识别为 0"),
            ("91999901MA8C2K7Q5X0", "统一社会信用代码应为18位，识别为19位"),
            ("91999901MA8C2K7Q5#", "含非法字符 #"),
        )
        config = self.registry["license"].fields[0]
        for pattern in (None, r"[0-9A-HJ-NPQRTUWXY]{18}"):
            config.pattern = pattern
            for code, error in cases:
                with self.subTest(pattern=pattern, code=code):
                    lines = [OcrLine(prefix + code, region, 0.9972,
                                     [(len(prefix), len(prefix) + len(code), value_box)])]
                    result = self.result("license", lines)
                    field = result.fields[0]
                    self.assertEqual(field.value, code)
                    self.assertEqual(field.bbox, value_box)
                    self.assertEqual(field.confidence, 0.9972)
                    self.assertTrue(field.needs_review)
                    self.assertEqual(field.validation.errors, [error])
                    self.assertIn(f"统一社会信用代码：校验失败（{error}）", result.issues)

    def test_gender_and_ethnicity_in_same_box(self):
        for lines, gender, ethnicity in (
            ([OcrLine("性别", [0.02, 0.25, 0.09, 0.31]),
              OcrLine("女民族汉", [0.12, 0.24, 0.37, 0.32], 0.9985)], "女", "汉"),
            ([OcrLine("性别男 民族壮", [0.00, 0.22, 0.26, 0.28], 0.9997)], "男", "壮"),
            ([OcrLine("性别", [0.02, 0.25, 0.09, 0.31]),
              OcrLine("民族汉", [0.12, 0.24, 0.37, 0.32], 0.9985)], "", "汉"),
        ):
            with self.subTest(gender=gender):
                result = self.result("id_front", lines)
                fields = {field.name: field for field in result.fields}
                self.assertEqual(fields["性别"].value, gender)
                self.assertEqual(fields["民族"].value, ethnicity)
                self.assertEqual(fields["性别"].needs_review, not gender)
                if not gender:
                    self.assertIn("性别：未提取到值", result.issues)
                else:
                    self.assertLess(fields["性别"].bbox[2], fields["民族"].bbox[0])

    def test_paddle_word_regions_keep_only_the_legal_representative_name(self):
        value = "鲍思衡"
        line = [[100, 200], [580, 200], [580, 250], [100, 250]]
        label = [[100, 200], [360, 200], [360, 250], [100, 250]]
        name = [[385, 200], [578, 200], [578, 250], [385, 250]]
        result = {"rec_texts": ["法定代表人 " + value], "rec_scores": [0.9823],
                  "rec_polys": [line], "text_word": [["法定代表人", value]],
                  "text_word_region": [[label, name]]}
        lines = PaddleCardEngine._result_lines(result, width=1000, height=1000, x=10, y=20)
        field = next(f for f in self.result("license", lines).fields if f.name == "法定代表人")
        self.assertEqual(field.value, value)
        self.assertEqual(field.bbox, [0.395, 0.22, 0.588, 0.27])
        self.assertEqual(field.confidence, 0.9823)

    def test_license_business_period_extraction_and_validation(self):
        cases = (
            ("2019年8月26日至长期", ""),
            ("2019.08.26-2030.08.26", ""),
            ("长期", ""),
            ("2021年2月30日至长期", "起始日期无法解析"),
            ("2020年1月5日至2019年1月5日", "顺序有误"),
            ("待定", "时间段格式异常"),
        )
        label_box = [0.06, 0.61, 0.20, 0.65]
        value_box = [0.25, 0.61, 0.54, 0.65]
        for value, expected_error in cases:
            with self.subTest(value=value):
                lines = [OcrLine("营业期限", label_box), OcrLine(value, value_box, 0.995)]
                result = self.result("license", lines)
                field = next(field for field in result.fields if field.name == "营业期限")
                self.assertEqual(field.value, value)
                self.assertEqual(field.bbox, value_box)
                self.assertEqual(field.needs_review, bool(expected_error))
                self.assertEqual(field.validation.passed, not expected_error)
                if expected_error:
                    self.assertIn(expected_error, "；".join(field.validation.errors))

    def test_business_period_is_omitted_only_without_its_label(self):
        label_box = [0.06, 0.61, 0.20, 0.65]
        without = self.result("license", [OcrLine("营业期服", label_box)])
        self.assertNotIn("营业期限", [field.name for field in without.fields])
        with_label = self.result("license", [OcrLine("经营期限", label_box)])
        field = next(field for field in with_label.fields if field.name == "营业期限")
        self.assertEqual(field.value, "")
        self.assertTrue(field.needs_review)
        self.assertIn("营业期限：未提取到值", with_label.issues)

    def test_overlapping_date_value_is_kept_and_cross_checked(self):
        date_box = [0.120, 0.374, 0.414, 0.436]
        for id_day in ("23", "24"):
            with self.subTest(id_day=id_day):
                lines = [OcrLine("出生", [0.027, 0.376, 0.139, 0.434]),
                         OcrLine("1992年7月23日", date_box),
                         OcrLine(f"公民身份号码999901199207{id_day}284X",
                                 [0.02, 0.8, 0.8, 0.85])]
                fields = {field.name: field for field in self.result("id_front", lines).fields}
                date = fields["出生日期"]
                self.assertEqual(date.value, "1992年7月23日")
                self.assertEqual(date.bbox, date_box)
                self.assertEqual(date.validation.passed, id_day == "23")
                self.assertEqual(date.needs_review, id_day != "23")

    def test_date_must_extend_to_the_right_of_the_label(self):
        lines = [OcrLine("出生", [0.027, 0.376, 0.139, 0.434]),
                 OcrLine("1992年7月23日", [0.120, 0.374, 0.149, 0.436])]
        fields = {field.name: field for field in self.result("id_front", lines).fields}
        self.assertEqual(fields["出生日期"].value, "")

    def test_large_watermark_box_cannot_mix_capital_and_establishment_date(self):
        amount_box = [0.3303, 0.4344, 0.5121, 0.4778]
        amount_word_box = [0.339, 0.4344, 0.5121, 0.4778]
        lines = [OcrLine("注册资本", [0.0591, 0.4389, 0.1674, 0.4844]),
                 OcrLine("壹仟贰佰万圆整", amount_box, 0.9907, [(0, 7, amount_word_box)]),
                 OcrLine("2017年1年7月", [0.3167, 0.4544, 0.5742, 0.5922], 0.796),
                 OcrLine("成立日期", [0.0591, 0.5033, 0.1697, 0.5567]),
                 OcrLine("住所", [0.0591, 0.5733, 0.1220, 0.6267]),
                 OcrLine("示例地址", [0.3311, 0.5689, 0.7106, 0.6156],
                         word_boxes=[(0, 4, [0.3400, 0.5689, 0.7106, 0.6156])])]
        attempted = []

        def recheck(bbox, multi_line):
            if 0.48 < bbox[1] < bbox[3] < 0.57 and not multi_line:
                attempted.append(bbox)
                return [OcrLine("2017年11月17日", bbox, 0.9829)]
            return []

        result = build_card_result("license.jpg", lines, self.registry, sha256="0" * 64,
                                   hint_type="license", recognize_region=recheck)
        fields = {field.name: field for field in result.fields}
        self.assertEqual(fields["注册资本"].value, "壹仟贰佰万圆整")
        self.assertEqual(fields["注册资本"].bbox, amount_word_box)
        self.assertEqual(fields["注册资本"].confidence, 0.9907)
        self.assertEqual(fields["成立日期"].value, "2017年11月17日")
        self.assertFalse(fields["成立日期"].needs_review)
        self.assertEqual(len(attempted), 1)
        self.assertLess(attempted[0][0], 0.317)
        self.assertEqual(fields["成立日期"].bbox, attempted[0])


class ZeroConfidenceRecheckTest(unittest.TestCase):
    def setUp(self):
        self.registry = load_certs(ROOT / "config/certs")
        self.lines = [
            OcrLine("证号", [0.07, 0.10, 0.15, 0.16]),
            OcrLine("999901200003151146", [0.32, 0.11, 0.64, 0.16]),
            OcrLine("国籍", [0.08, 0.36, 0.15, 0.42]),
            OcrLine("中国", [0.32, 0.36, 0.39, 0.42]),
            OcrLine("出生日期", [0.085, 0.45, 0.216, 0.503]),
            OcrLine("住址", [0.08, 0.54, 0.16, 0.59]),
            OcrLine("示例省示例市", [0.32, 0.54, 0.70, 0.59]),
            OcrLine("初次领证日期", [0.09, 0.71, 0.28, 0.75]),
            OcrLine("2015年5月23日", [0.32, 0.71, 0.54, 0.75]),
        ]
        self.frame = object()
        self.engine = Mock()
        self.engine.recognize_frame.return_value = self.lines, self.frame
        self.engine.recognize_lines.return_value = self.lines

    def fields(self, **kwargs):
        result = process_image("driver.jpg", card_engine=self.engine, document_reader=None,
                               registry=self.registry, sha256="0" * 64, hint_type="driver",
                               **kwargs)["result"]
        return {field["name"]: field for field in result["fields"]}

    def test_missing_date_uses_matching_frame_and_parses_watermark_suffix(self):
        self.engine.recognize_region.side_effect = (
            lambda frame, bbox, multi: [OcrLine("2000年3月15日本", bbox, 0.9571898)]
        )
        birth = self.fields()["出生日期"]
        self.assertEqual(birth["value"], "2000年3月15日")
        self.assertEqual(birth["confidence"], 0.9572)
        self.assertFalse(birth["needs_review"])
        self.engine.recognize_region.assert_called_once()
        frame, bbox, multi = self.engine.recognize_region.call_args.args
        self.assertIs(frame, self.frame)
        self.assertFalse(multi)
        self.assertGreater(bbox[0], 0.216)
        self.assertLess(bbox[3], 0.54)
        self.assertEqual(birth["bbox"][:2], bbox[:2])
        self.assertEqual(birth["bbox"][3], bbox[3])
        self.assertLess(birth["bbox"][2], bbox[2])

    def test_other_zero_fields_are_rechecked_once_and_multiline_is_enabled(self):
        self.lines[3].confidence = 0.0
        self.lines[6].confidence = 0.0
        self.lines.insert(5, OcrLine("2000年3月15日", [0.32, 0.45, 0.53, 0.503]))
        self.engine.recognize_region.side_effect = (
            lambda frame, bbox, multi: [OcrLine("示例省", bbox, 0.98), OcrLine("示例市", bbox, 0.96)]
            if multi else [OcrLine("中国", bbox, 0.99)]
        )
        result = self.fields()
        self.assertEqual(result["国籍"]["confidence"], 0.99)
        self.assertEqual(result["住址"]["value"], "示例省示例市")
        self.assertEqual(result["住址"]["confidence"], 0.96)
        self.assertEqual([call.args[2] for call in self.engine.recognize_region.call_args_list],
                         [False, True])

    def test_low_confidence_or_conflicting_candidate_stays_in_review(self):
        for text, score in (("2000年3月15日", 0.69), ("2000年3月16日", 0.99)):
            with self.subTest(text=text, score=score):
                self.engine.recognize_region.return_value = [OcrLine(text, [0.32, 0.45, 0.53, 0.5], score)]
                birth = self.fields()["出生日期"]
                self.assertEqual(birth["value"], "")
                self.assertTrue(birth["needs_review"])

    def test_whole_image_retry_does_not_repeat_local_recheck(self):
        self.assertTrue(self.fields(card_det_side=1280, recheck_zero_fields=False)["出生日期"]["needs_review"])
        self.engine.recognize_lines.assert_called_once_with(Path("driver.jpg"), det_side=1280)
        self.engine.recognize_region.assert_not_called()


class RecognitionReviewTest(unittest.TestCase):
    def setUp(self):
        self.registry = load_certs(ROOT / "config/certs")

    def test_merged_single_line_fields_stop_at_next_label(self):
        lines = [OcrLine("姓名示例性别男", [0.1, 0.1, 0.5, 0.15]),
                 OcrLine("民族壮出生日期2000年1月1日", [0.1, 0.2, 0.8, 0.25])]
        result = build_card_result("x.jpg", lines, self.registry, sha256="fixture", doc_type="id_front")
        fields = {field.name: field for field in result.fields}
        self.assertEqual(fields["姓名"].value, "示例")
        self.assertEqual(fields["民族"].value, "壮")

    def test_id_and_driver_checksum_errors_are_reported_without_changing_values(self):
        for kind, name, value, expected in (
            ("id_front", "公民身份号码", "99990119920723684X", "0"),
            ("driver", "证号", "999901198512095873", "5"),
        ):
            with self.subTest(kind=kind):
                lines = [OcrLine(name + value, [0.1, 0.2, 0.9, 0.25])]
                result = build_card_result("x.jpg", lines, self.registry, sha256="fixture", doc_type=kind)
                field = next(field for field in result.fields if field.name == name)
                self.assertEqual(field.value, value)
                self.assertTrue(field.needs_review)
                self.assertIn(f"期望 {expected}", field.validation.errors[0])

    def test_scope_punctuation_is_reviewed_and_only_corrected_from_a_second_read(self):
        garbled = "甲类业务：乙类业务：丙类业务3.5千瓦（依法须经批准的项目。经相关部门批准）"
        corrected = "甲类业务；乙类业务；丙类业务3.5千瓦（依法须经批准的项目，经相关部门批准）"
        box = [0.1, 0.3, 0.9, 0.4]
        lines = [OcrLine("经营范围" + garbled, box, 0.99)]
        for retry_value, expected, review in (
            (garbled, garbled, True),
            (corrected.replace("乙类", "丁类"), garbled, True),
            (corrected.replace("3.5", "35"), garbled, True),
            (corrected, corrected, False),
        ):
            with self.subTest(retry_value=retry_value):
                recognize = Mock(return_value=[OcrLine(retry_value, box, 0.90)])
                result = build_card_result("x.jpg", lines, self.registry, sha256="fixture",
                                           doc_type="license", recognize_region=recognize)
                scope = next(field for field in result.fields if field.name == "经营范围")
                self.assertEqual((scope.value, scope.needs_review), (expected, review))
                recognize.assert_called_once()

    def test_engine_receives_pixels_and_does_not_call_model_on_decode_failure(self):
        from PIL import Image
        from app.engine.image_io import InvalidImageError

        engine = PaddleCardEngine.__new__(PaddleCardEngine)
        engine._lock = threading.Lock()
        engine._predict_with_word_boxes = Mock(return_value=[])
        image = io.BytesIO()
        Image.new("RGB", (128, 128), (255, 0, 0)).save(image, format="JPEG")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "sample.jpg"
            path.write_bytes(image.getvalue())
            engine.recognize_frame(path)
            pixels = engine._predict_with_word_boxes.call_args.args[0]
            self.assertEqual(pixels.shape, (128, 128, 3))
            self.assertGreater(pixels[0, 0, 2], pixels[0, 0, 0])
            engine._predict_with_word_boxes.reset_mock()
            path.write_bytes(image.getvalue()[:-80])
            with self.assertRaises(InvalidImageError):
                engine.recognize_frame(path, rectified_out=Path(temp) / "preview.jpg")
            engine._predict_with_word_boxes.assert_not_called()
            self.assertFalse((Path(temp) / "preview.jpg").exists())


class FuzzyAnchorTest(unittest.TestCase):
    """锚点容错匹配：畸变下单字误认（注册资本→主册资本）不再让字段落空。"""

    def test_misread_label_picks_right_value(self):
        cfg = load_certs(ROOT / "config/certs")["license"]
        lines = [OcrLine("主册资本", [0.01, 0.22, 0.2, 0.26]),
                 OcrLine("壹仟贰佰万圆整", [0.3, 0.22, 0.6, 0.26])]
        fields = {f.name: f for f in assemble_fields(lines, cfg)}
        self.assertEqual(fields["注册资本"].value, "壹仟贰佰万圆整")

    def test_misread_label_same_line_stripped(self):
        """标签与值同一行且标签误读：去掉误读标签段，值不含'主册资本'。"""
        cfg = load_certs(ROOT / "config/certs")["license"]
        lines = [OcrLine("主册资本：伍佰万圆整", [0.01, 0.22, 0.7, 0.26])]
        fields = {f.name: f for f in assemble_fields(lines, cfg)}
        self.assertEqual(fields["注册资本"].value, "伍佰万圆整")

    def test_exact_match_still_wins_over_fuzzy(self):
        """图里同时有误读行和精确行时，精确行优先。"""
        cfg = load_certs(ROOT / "config/certs")["license"]
        lines = [OcrLine("注册资本", [0.01, 0.10, 0.2, 0.14]),
                 OcrLine("壹佰万圆整", [0.3, 0.10, 0.6, 0.14]),
                 OcrLine("主册资本", [0.01, 0.22, 0.2, 0.26]),
                 OcrLine("贰佰万圆整", [0.3, 0.22, 0.6, 0.26])]
        fields = {f.name: f for f in assemble_fields(lines, cfg)}
        self.assertEqual(fields["注册资本"].value, "壹佰万圆整")

    def test_ambiguous_misread_rejected(self):
        """跨字段歧义放弃：'类称'距'类型'和'名称'都是 1 字，谁也不能抢。"""
        line = [OcrLine("类称", [0.01, 0.1, 0.2, 0.14])]
        hit = _fuzzy_anchor_hit(line, ["类型"], all_anchors={"类型", "名称"})
        self.assertIsNone(hit)
        # 无跨字段上下文（单字段视角）时正常命中
        self.assertIsNotNone(_fuzzy_anchor_hit(line, ["类型"]))

    def test_alias_tie_prefers_longer_anchor(self):
        """同字段别名同分（都差 1 字）→ 取更长更具体的锚点。"""
        hit = _fuzzy_anchor_hit(
            [OcrLine("统一社会信用代础", [0.0, 0.1, 0.4, 0.14])],
            ["统一社会信用代码", "信用代码"])
        self.assertIsNotNone(hit)
        self.assertEqual(hit[1], "统一社会信用代码")

    def test_two_char_misread_within_long_anchor(self):
        """长锚点（≥6 字）容错 2 字：'统一让会售用代码'仍命中。"""
        hit = _fuzzy_anchor_hit(
            [OcrLine("统一让会售用代码", [0.0, 0.1, 0.4, 0.14])],
            ["统一社会信用代码"])
        self.assertIsNotNone(hit)
        self.assertEqual(hit[1], "统一社会信用代码")


if __name__ == "__main__":
    unittest.main()
