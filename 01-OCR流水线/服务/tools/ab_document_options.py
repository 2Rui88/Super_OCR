"""PP-StructureV3 配置 A/B 实验：印章识别 / 方向矫正 开关对扫描件的效果对比。

每个变体独立加载引擎跑同一份 PDF，对照 golden 计算字级准确率、单元格匹配、
每页耗时，输出对比报告。结论规则：没有准确率收益的开关不进默认配置。

用法（服务目录，CPU 空闲时跑）：
    .venv/Scripts/python.exe tools/ab_document_options.py \
        --pdf "../资料/扫描文档/印章管理规定2021（盖章扫描件）.pdf" \
        --golden "../资料/扫描文档/golden/印章管理规定2021（盖章扫描件）.json" \
        --out tests/_out/ab_印章开关对比.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

VARIANTS = {
    "default": {},                                   # 现行默认：印章开，方向/矫正关
    "seal_on": {"use_seal_recognition": True},
    "orientation_on": {"use_doc_orientation_classify": True, "use_doc_unwarping": True},
    "seal_and_orientation": {"use_seal_recognition": True,
                             "use_doc_orientation_classify": True,
                             "use_doc_unwarping": True},
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--golden", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--variants", nargs="*", default=list(VARIANTS),
                        choices=list(VARIANTS))
    args = parser.parse_args()

    from app.core.document_pipeline import PaddleDocumentEngine, process_document
    from eval_documents import evaluate_document

    golden = json.loads(args.golden.read_text(encoding="utf-8"))
    report = {"pdf": args.pdf.name, "variants": {}}
    for name in args.variants:
        engine = PaddleDocumentEngine(extra_options=VARIANTS[name])
        started = time.perf_counter()
        result = process_document(args.pdf, scan_reader=engine)
        wall = time.perf_counter() - started
        engine.close()
        metrics = evaluate_document(result.model_dump(mode="json"), golden)
        metrics["wall_seconds"] = round(wall, 1)
        report["variants"][name] = metrics
        print(f"{name:22s} 字级={metrics['char_accuracy_mean']} "
              f"单元格={metrics['cell_match_rate']} 结构={metrics['table_structure_rate']} "
              f"每页均值={metrics['duration_ms_total']/max(1,len(metrics['pages'])):.0f}ms "
              f"墙钟={wall:.0f}s", flush=True)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"→ {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
