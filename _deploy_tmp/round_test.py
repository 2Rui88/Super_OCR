"""A/B 压测单轮驱动:30 证件批传 + 6 份 PDF 并发,采集吞吐/等待分位/页耗时/利用率/显存。

用法: .venv/bin/python /tmp/round_test.py <round_name>
测试文件为「原文件 + 尾部随机字节」的唯一副本 → 内容哈希唯一 → 不命中结果缓存,
每轮、每个任务都走真实 GPU OCR。JPG/PDF 解码器均忽略 EOI/%%EOF 之后的数据。
"""
import json
import re
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import httpx

API = 'http://127.0.0.1:8700'
ROOT = Path('/root/rivermind-data/01-OCR流水线')
DB = ROOT / '服务/data/tasks.sqlite3'
NAME = sys.argv[1]
WORK = Path('/tmp/abtest') / NAME
CARDS_N = 30
DOCS_N = 6


def build_unique_set():
    (WORK / 'cards').mkdir(parents=True, exist_ok=True)
    (WORK / 'docs').mkdir(exist_ok=True)
    cards = sorted((ROOT / '资料/自造测试集').glob('*.jpg'))[:CARDS_N]
    assert len(cards) == CARDS_N, f'only {len(cards)} cards'
    for i, c in enumerate(cards, 1):
        (WORK / 'cards' / f'c{i:02d}.jpg').write_bytes(
            c.read_bytes() + f'\n#load-{NAME}-{i}\n'.encode())
    pdfs = sorted((ROOT / '资料/扫描文档').glob('*.pdf'))
    for i in range(DOCS_N):
        p = pdfs[i % len(pdfs)]
        (WORK / 'docs' / f'd{i+1}.pdf').write_bytes(
            p.read_bytes() + f'\n%load-{NAME}-{i}\n'.encode())


def parse_ts(s):
    return datetime.fromisoformat(s)


def main():
    build_unique_set()
    # GPU 采样:dmon 利用率 + 显存峰值,各 1 秒一次
    dmon = subprocess.Popen(
        ['nvidia-smi', 'dmon', '-s', 'u', '-d', '1'],
        stdout=open(WORK / 'dmon.log', 'w'), stderr=subprocess.DEVNULL)
    vram_stop = threading.Event()
    vram_peak = [0]

    def vram_sampler():
        while not vram_stop.is_set():
            try:
                out = subprocess.check_output(
                    ['nvidia-smi', '--query-gpu=memory.used',
                     '--format=csv,noheader,nounits'], text=True)
                vram_peak[0] = max(vram_peak[0], int(out.strip().splitlines()[0]))
            except Exception:
                pass
            time.sleep(1)
    threading.Thread(target=vram_sampler, daemon=True).start()

    client = httpx.Client(timeout=300, limits=httpx.Limits(
        keepalive_expiry=4, max_connections=20))
    t0 = time.perf_counter()
    r = client.post(f'{API}/api/v1/batches', files=[
        ('files', (f.name, f.read_bytes(), 'image/jpeg'))
        for f in sorted((WORK / 'cards').glob('*.jpg'))])
    r.raise_for_status()
    bid = r.json()['batch_id']

    doc_ids, doc_errs = [], []

    def submit(p):
        try:
            rr = client.post(f'{API}/api/v1/documents',
                             files={'file': (p.name, p.read_bytes(), 'application/pdf')})
            rr.raise_for_status()
            doc_ids.append(rr.json()['task_id'])
        except Exception as e:  # noqa: BLE001
            doc_errs.append(f'{p.name}: {e}')
    threads = [threading.Thread(target=submit, args=(p,))
               for p in sorted((WORK / 'docs').glob('*.pdf'))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    while True:
        b = client.get(f'{API}/api/v1/batches/{bid}').json()['counts']
        if b['done'] + b['failed'] >= CARDS_N:
            docs_ok = True
            for tid in doc_ids:
                st = client.get(f'{API}/api/v1/tasks/{tid}').json()['status']
                if st not in ('done', 'failed'):
                    docs_ok = False
            if docs_ok:
                break
        time.sleep(0.3)
    wall = time.perf_counter() - t0

    dmon.terminate()
    vram_stop.set()
    time.sleep(0.5)

    # ---- 指标采集 ----
    import sqlite3
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "select t.* from tasks t join batch_items bi on bi.task_id=t.id "
        "where bi.batch_id=? and t.task_type='card'", (bid,)).fetchall()
    waits, services = [], []
    card_failed = 0
    for row in rows:
        if row['status'] != 'done':
            card_failed += 1
            continue
        waits.append((parse_ts(row['finished_at']) - parse_ts(row['created_at'])).total_seconds())
        services.append((parse_ts(row['finished_at']) - parse_ts(row['started_at'])).total_seconds())
    doc_page_ms, doc_walls, doc_failed = [], [], 0
    for tid in doc_ids:
        row = conn.execute('select * from tasks where id=?', (tid,)).fetchone()
        if row['status'] != 'done':
            doc_failed += 1
            continue
        doc_walls.append((parse_ts(row['finished_at']) - parse_ts(row['created_at'])).total_seconds())
        res = json.loads(row['result_json'])
        for pg in res.get('result', {}).get('pages', []):
            doc_page_ms.append(pg.get('duration_ms') or 0)

    sm_vals = []
    for line in (WORK / 'dmon.log').read_text().splitlines():
        m = re.match(r'^\s*\d+\s+(\d+)\s+(\d+)', line)
        if m:
            sm_vals.append(int(m.group(1)))

    def pct(xs, p):
        if not xs:
            return None
        xs = sorted(xs)
        return round(xs[min(len(xs) - 1, int(len(xs) * p / 100))], 3)

    summary = {
        'round': NAME, 'batch_id': bid, 'wall_s': round(wall, 2),
        'cards': {'n': len(rows), 'failed': card_failed,
                  'throughput_per_s': round(len(waits) / wall, 2),
                  'wait_p50_s': pct(waits, 50), 'wait_p95_s': pct(waits, 95),
                  'wait_max_s': round(max(waits), 3) if waits else None,
                  'service_p50_s': pct(services, 50),
                  'service_p95_s': pct(services, 95)},
        'docs': {'n': len(doc_ids), 'failed': doc_failed, 'submit_errs': doc_errs,
                 'wall_p50_s': pct(doc_walls, 50), 'wall_max_s': round(max(doc_walls), 2) if doc_walls else None,
                 'pages': len(doc_page_ms),
                 'page_ms_avg': round(statistics.mean(doc_page_ms)) if doc_page_ms else None,
                 'page_ms_p95': pct(doc_page_ms, 95)},
        'gpu': {'sm_avg': round(statistics.mean(sm_vals)) if sm_vals else None,
                'sm_max': max(sm_vals) if sm_vals else None,
                'vram_peak_mib': vram_peak[0], 'dmon_samples': len(sm_vals)},
    }
    (WORK / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
