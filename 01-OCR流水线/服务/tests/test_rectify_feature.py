"""摆正视图特性的离线回归:文件名守卫与契约字段(不加载 OCR 模型)。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.api.routes_files import _NAME_RE  # noqa: E402
from app.contracts import CardResult  # noqa: E402

SHA = "a" * 64


class RectifiedFileNameTest(unittest.TestCase):
    def test_rect_jpg_accepted(self):
        self.assertIsNotNone(_NAME_RE.match(f"{SHA}.rect.jpg"))

    def test_plain_upload_still_accepted(self):
        self.assertIsNotNone(_NAME_RE.match(f"{SHA}.jpg"))
        self.assertIsNotNone(_NAME_RE.match(f"{SHA}.pdf"))

    def test_traversal_rejected(self):
        for bad in (f"../{SHA}.rect.jpg", f"{SHA}.rect.jpg/../../x.jpg",
                    f"{SHA}.rect.html", f"{SHA}.rect", "x" * 60 + ".rect.jpg"):
            self.assertIsNone(_NAME_RE.match(bad), bad)


class ContractFieldTest(unittest.TestCase):
    def test_card_result_has_optional_rectified_file(self):
        result = CardResult(file_sha256=SHA, filename="a.jpg", doc_type="id_front")
        self.assertIsNone(result.rectified_file)
        dumped = result.model_dump(mode="json")
        self.assertIn("rectified_file", dumped)


if __name__ == "__main__":
    unittest.main()
