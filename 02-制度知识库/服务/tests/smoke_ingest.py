"""摄入冒烟：走真实链路（P2 收件 → 队列 → P1 识别 → 分块入库 → 就绪）。

用法：.venv/Scripts/python.exe tests/smoke_ingest.py <文件路径>...
可选环境：KB_API（默认 http://127.0.0.1:8800）
"""
from __future__ import annotations

import os
import sys
import time

import httpx

BASE = os.environ.get("KB_API", "http://127.0.0.1:8800/api/v1")


def main() -> int:
    files = sys.argv[1:]
    if not files:
        print("用法: smoke_ingest.py <文件>...")
        return 2
    with httpx.Client(timeout=60) as client:
        # 清掉历史同 sha 记录，保证走全新导入
        resp = client.post(f"{BASE}/documents",
                           files=[("files", (os.path.basename(p), open(p, "rb"),
                                             "application/octet-stream")) for p in files])
        print("导入:", resp.status_code)
        for item in resp.json()["results"]:
            print(" ", item)
        doc_ids = [item["doc_id"] for item in resp.json()["results"] if item.get("doc_id")]
        deadline = time.time() + 1800
        while time.time() < deadline:
            docs = client.get(f"{BASE}/documents").json()["documents"]
            watch = [d for d in docs if d["id"] in doc_ids]
            line = " | ".join(f"#{d['id']} {d['status']} {d['phase']}" for d in watch)
            print(f"\r{line:<80}", end="", flush=True)
            if all(d["status"] in ("ready", "failed") for d in watch):
                print()
                for d in watch:
                    print(f"#{d['id']} {d['filename']} -> {d['status']}"
                          f" 页={d['page_count']} 表={d['table_count']} 图={d['figure_count']}"
                          f" err={d['error'][:80]}")
                    if d["status"] == "ready":
                        detail = client.get(f"{BASE}/documents/{d['id']}").json()
                        print(f"   目录: {[t['heading'] for t in detail['toc']][:8]}")
                        print(f"   块数: {len(detail['chunks'])}（首块: "
                              f"{detail['chunks'][0]['seek_text'][:40]!r}）" if detail["chunks"] else "   无块")
                return 0 if all(d["status"] == "ready" for d in watch) else 1
            time.sleep(5)
    return 1


if __name__ == "__main__":
    sys.exit(main())
