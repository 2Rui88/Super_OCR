"""用本地视觉转写文件生成文档评测基准。

输入是按页排列的 JSON 数组，每页包含 text 和 tables。
可用 --out 将生成结果保存到仓库之外。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="生成文档 OCR 评测基准")
    parser.add_argument("--file", required=True, help="源文档名称")
    parser.add_argument("--pages", type=Path, required=True, help="本地逐页转写 JSON")
    parser.add_argument("--out", type=Path, required=True, help="评测基准 JSON 的输出路径")
    args = parser.parse_args()

    pages = json.loads(args.pages.read_text(encoding="utf-8-sig"))
    if not isinstance(pages, list) or not pages or any(not isinstance(page, dict) for page in pages):
        parser.error("--pages 必须是非空的 JSON 页面对象数组")

    golden = {
        "file": args.file,
        "provenance": "ai_vision_transcript",
        "pages": [
            {"page_num": index, "mode": "ocr", **page}
            for index, page in enumerate(pages, start=1)
        ],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(golden, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{args.out.name}: {len(pages)} 页")


if __name__ == "__main__":
    main()
