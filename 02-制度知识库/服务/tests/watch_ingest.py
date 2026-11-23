"""守望 docs>=5 至全部终态，打印状态分布；全就绪退出 0，有失败退出 1。"""
import sys
import time

import httpx

BASE = "http://127.0.0.1:8800/api/v1"
deadline = time.time() + 55 * 60
while time.time() < deadline:
    try:
        docs = [d for d in httpx.get(f"{BASE}/documents", timeout=10).json()["documents"]
                if d["id"] >= 5]
        counts = {}
        for d in docs:
            counts[d["status"]] = counts.get(d["status"], 0) + 1
        line = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        print(time.strftime("%H:%M:%S"), line, flush=True)
        if all(d["status"] in ("ready", "failed") for d in docs):
            for d in docs:
                print(d["id"], d["status"], d["filename"][:24],
                      "| 页", d["page_count"], "| 表", d["table_count"],
                      "| 图", d["figure_count"],
                      "| 错:", d["error"][:60] or "无")
            sys.exit(0 if all(d["status"] == "ready" for d in docs) else 1)
    except Exception as exc:
        print(time.strftime("%H:%M:%S"), "poll error:", exc, flush=True)
    time.sleep(45)
print("TIMEOUT")
sys.exit(2)
