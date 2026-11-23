"""证件字段组装的几何回归(不加载 OCR 模型)。

覆盖 assemble_fields 的空间匹配规则:多行值吞行、水印行剔除、
相邻列标签作为吞行边界。原出自 test_optimizations.py(旧性能战役遗留
混合文件),拆分后仅保留与当前链路相关的组装回归。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.certs import load_certs  # noqa: E402
from app.core.card_pipeline import _fuzzy_anchor_hit, assemble_fields  # noqa: E402
from app.engine.base import OcrLine  # noqa: E402


class MultilineAssemblyTest(unittest.TestCase):
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
