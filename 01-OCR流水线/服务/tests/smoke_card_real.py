"""D0 冒烟测试（真引擎）：PaddleOCR 跑公开样例证件。

首次运行会自动下载配置中指定的模型。运行：
python tests/smoke_card_real.py [图片路径 ...]
不传参数则默认跑全部公开样例。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SAMPLES = ROOT.parent / "资料" / "公开样例"
GT = json.loads((SAMPLES / "标准答案.json").read_text(encoding="utf-8"))


def main() -> int:
    from app.certs import load_certs
    from app.core.card_pipeline import build_card_result
    from app.engine.paddle_card import PaddleCardEngine

    registry = load_certs(ROOT / "config" / "certs")
    t0 = time.perf_counter()
    engine = PaddleCardEngine()
    print(f"引擎初始化（含模型下载/加载）：{time.perf_counter()-t0:.1f}s\n")

    targets = [Path(a) for a in sys.argv[1:]] or sorted(SAMPLES.glob("*.jpg"))
    gt_by_file = {r["file"]: r for r in GT}

    total_fields, correct_fields = 0, 0
    results_by_file: dict[str, object] = {}
    for img in targets:
        t1 = time.perf_counter()
        lines = engine.recognize_lines(img)
        result = build_card_result(img, lines, registry)
        results_by_file[img.name] = result
        wall = time.perf_counter() - t1
        gt = gt_by_file.get(img.name, {})
        expected_fields = gt.get("fields", {})
        actual_fields = {field.name: field.value for field in result.fields}
        total_fields += len(expected_fields)
        correct_fields += sum(
            result.doc_type == gt.get("doc") and actual_fields.get(name, "").strip() == str(value).strip()
            for name, value in expected_fields.items()
        )
        print(f"── {img.name}  [{wall:.1f}s]")
        print(f"   类型: {result.doc_type_cn}({result.doc_type}, conf={result.type_confidence})"
              f"  期望: {gt.get('doc_cn', '?')}")
        for f in result.fields:
            expect = gt.get("fields", {}).get(f.name)
            mark = ""
            if expect is not None:
                hit = f.value.strip() == str(expect).strip()
                mark = "✓" if hit else f"✗ 期望'{expect}'"
            flag = " ⚠️有问题" if f.needs_review else ""
            print(f"   {f.name}: '{f.value}' conf={f.confidence}{mark}{flag}")
        if result.issues:
            for iss in result.issues:
                print(f"   [问题] {iss}")
        print()

    if total_fields:
        print(f"字段准确率（vs 标准答案）：{correct_fields}/{total_fields} "
              f"= {correct_fields/total_fields:.1%}")
    # 结果落盘，避免管道截断丢失明细
    out = ROOT / "tests" / "_out" / "smoke_card_latest.json"
    out.parent.mkdir(exist_ok=True)
    payload = []
    for img in targets:
        r = results_by_file[img.name]
        payload.append({
            "file": img.name,
            "doc_type": r.doc_type, "type_confidence": r.type_confidence,
            "duration_ms": r.duration_ms, "issues": r.issues,
            "fields": [f.model_dump() for f in r.fields],
            "ground_truth": gt_by_file.get(img.name, {}).get("fields", {}),
        })
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"明细已存 {out}")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
