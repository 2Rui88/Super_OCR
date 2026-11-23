"""批量生成 50 张测试证件 —— 复用 gen_test_certs 的渲染/数据/扰动，仅换配额与输出目录。

用法（服务目录）：
    .venv/Scripts/python.exe tools/gen_bulk_certs.py [输出目录] [--seed 20261119]

配额：id_front 13 + id_back 12 + driver 13 + license 12 = 50。
每类先轮询自身条件池（含故意坏字段），不足配额用 scan/rotate 补齐；
产物与 标准答案.json 一起写入输出目录。
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gen_test_certs as g  # noqa: E402

PLAN = {"id_front": 13, "id_back": 12, "driver": 13, "license": 12}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", nargs="?", type=Path,
                        default=Path("C:/Users/Wren-GS/Desktop/OCR测试集/01_证件图片"))
    parser.add_argument("--seed", type=int, default=20261119)
    args = parser.parse_args()
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    renderer = g.Renderer()

    truth = []
    for doc, count in PLAN.items():
        rng = random.Random(f"{args.seed}:{doc}")
        conds = list(g.CONDITIONS[doc])
        while len(conds) < count:
            conds.append(g.FILLERS[len(conds) % len(g.FILLERS)])
        conds = conds[:count]
        for index, cond in enumerate(conds):
            data, fields, invalid = g.make_record(doc, rng, cond)
            image = getattr(renderer, doc)(data)
            image, _params = g.perturb(image, cond, rng)
            filename = f"{doc}_{index:02d}_{cond}.jpg"
            image.save(out / filename, quality=90)
            truth.append({"file": filename, "doc": doc, "doc_cn": g.DOC_CN[doc],
                          "cond": cond, "fields": fields,
                          **({"invalid_fields": invalid} if invalid else {})})
            print(f"{filename}  invalid={invalid or '-'}")

    (out / "标准答案.json").write_text(
        json.dumps(truth, ensure_ascii=False, indent=2), encoding="utf-8")

    total = len(truth)
    per_type = {kind: sum(1 for t in truth if t["doc"] == kind) for kind in PLAN}
    invalid_n = sum(1 for t in truth if t.get("invalid_fields"))
    print(f"\n共 {total} 张 → {out}")
    print("每类张数：", per_type)
    print(f"含故意坏字段的样本：{invalid_n} 张（bad_number/bad_date/bad_period）")
    assert total == 50 and sum(per_type.values()) == 50
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
