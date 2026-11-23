"""批量评测 CLI —— 整批上传目录图片 → 轮询批次 → 调 /eval → 报告落盘。

用法（服务目录，需 API 与至少一个 worker 在跑）：
    .venv/Scripts/python.exe tools/batch_eval_cli.py --dir ../资料/自造测试集 \
        --out tests/_out/自造测试集评测.json
也是项目二式的调用示例：上传 → 轮询 → 取结果。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:8700")
    parser.add_argument("--dir", type=Path, default=ROOT.parent / "资料" / "自造测试集")
    parser.add_argument("--gt", type=Path, default=None, help="标准答案，默认 <dir>/标准答案.json")
    parser.add_argument("--out", type=Path, default=ROOT / "tests/_out/自造测试集评测.json")
    args = parser.parse_args()

    import httpx

    def robust(client, method, url, **kwargs):
        """轮询会被 uvicorn 的 keep-alive 超时打断（连接恰在复用时被关），
        传输层错误重试即可；GET 天然幂等。"""
        import time as _time

        for attempt in range(5):
            try:
                return client.request(method, url, **kwargs)
            except httpx.TransportError:
                if attempt == 4:
                    raise
                _time.sleep(1 + attempt)

    gt_path = args.gt or (args.dir / "标准答案.json")
    images = sorted(args.dir.glob("*.jpg")) + sorted(args.dir.glob("*.png"))
    if not images:
        parser.error(f"{args.dir} 下没有图片")

    with httpx.Client(base_url=args.api, timeout=120,
                      limits=httpx.Limits(keepalive_expiry=4),
                      transport=httpx.HTTPTransport(retries=2)) as client:
        started = time.perf_counter()
        response = robust(client, "POST", "/api/v1/batches", files=[
            ("files", (path.name, path.read_bytes(), "image/jpeg")) for path in images])
        response.raise_for_status()
        batch_id = response.json()["batch_id"]
        print(f"批次 {batch_id}：{len(images)} 张已上传", flush=True)

        while True:
            batch = robust(client, "GET", f"/api/v1/batches/{batch_id}").json()
            counts = batch["counts"]
            finished = counts["done"] + counts["failed"]
            print(f"  进度 {finished}/{batch['total']}"
                  f"（done={counts['done']} failed={counts['failed']}）", flush=True)
            if counts["queued"] == 0 and counts["running"] == 0:
                break
            time.sleep(5)

        result = robust(client, "POST", "/api/v1/eval", data={"batch_id": batch_id},
                        files={"ground_truth": ("gt.json", gt_path.read_bytes(),
                                                "application/json")})
        result.raise_for_status()
        payload = result.json()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    report = payload["report"]
    print(f"\n批次 {batch_id} 完成，用时 {time.perf_counter() - started:.0f}s")
    print(f"字段准确率：{report['correct_fields']}/{report['total_fields']}"
          f" = {report['field_accuracy']:.2%}   类型准确率：{report['type_accuracy']:.2%}")
    print(f"故意做错字段检出率：{report['planted_invalid_flagged']}/"
          f"{report['planted_invalid_total']}"
          f" = {report['planted_invalid_flag_rate'] or 0:.2%}")
    if report["wrong_fields_total"]:
        print(f"抄错字段标出率：{report['wrong_fields_flagged']}/{report['wrong_fields_total']}"
              f" = {report['wrong_field_flag_rate']:.2%}")
    print(f"报告已写入 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
