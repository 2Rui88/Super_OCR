"""50 份非图片文档全链路测试:提交 → Office 转换 → 路由 → 提取,采集路由/耗时/内容量。"""
import json
import re
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

import httpx

API = 'http://127.0.0.1:8700'
SRC = Path('/root/rivermind-data/OCR测试集2/02_非图片文档')
DB = Path('/root/rivermind-data/01-OCR流水线/服务/data/tasks.sqlite3')
OUT = Path('/root/rivermind-data/01-OCR流水线/服务/tests/_out/非图片文档_测试集2_50份.json')

MIME = {
    '.pdf': 'application/pdf',
    '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
}

files = sorted(p for p in SRC.iterdir() if p.suffix.lower() in MIME)
assert len(files) == 50, f'expect 50, got {len(files)}'
tids = [None] * len(files)
errs = []


def submit(i, p):
    try:
        with httpx.Client(timeout=120) as c:
            r = c.post(f'{API}/api/v1/documents',
                       files={'file': (p.name, p.read_bytes(), MIME[p.suffix.lower()])})
            r.raise_for_status()
            tids[i] = r.json()['task_id']
    except Exception as e:  # noqa: BLE001
        errs.append(f'{p.name}: {e}')


t0 = time.perf_counter()
threads = [threading.Thread(target=submit, args=(i, p)) for i, p in enumerate(files)]
for t in threads:
    t.start()
for t in threads:
    t.join()

client = httpx.Client(timeout=60)
while True:
    stat = {}
    for tid in filter(None, tids):
        st = client.get(f'{API}/api/v1/tasks/{tid}').json()['status']
        stat[st] = stat.get(st, 0) + 1
    print('poll:', stat, flush=True)
    if stat.get('done', 0) + stat.get('failed', 0) == len(files):
        break
    time.sleep(2)
wall = time.perf_counter() - t0


def parse_ts(s):
    return datetime.fromisoformat(s)


def cjk_count(s):
    return len(re.findall(r'[\u4e00-\u9fff]', s or ''))


def doc_text(res):
    """正文在 pages[].blocks[](text/row_texts),结果无 markdown 键。"""
    parts = []
    for pg in res.get('pages', []):
        for b in pg.get('blocks', []):
            parts.append(b.get('text') or '')
            for row in b.get('row_texts') or []:
                parts.extend(row if isinstance(row, list) else [row])
    return '\n'.join(parts)


conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
rows = []
for p, tid in zip(files, tids):
    r = conn.execute('select * from tasks where id=?', (tid,)).fetchone()
    item = {'file': p.name, 'ext': p.suffix, 'task_id': tid,
            'status': r['status'], 'route': r['task_type']}
    if r['status'] == 'done':
        res = json.loads(r['result_json'])['result']
        pages = res.get('pages', [])
        text = doc_text(res)
        item.update({
            'pages': len(pages),
            'wall_s': round((parse_ts(r['finished_at']) - parse_ts(r['created_at'])).total_seconds(), 2),
            'page_ms_avg': round(sum(pg.get('duration_ms') or 0 for pg in pages) / len(pages)) if pages else None,
            'total_ms': res.get('total_duration_ms'),
            'md_chars': len(text),
            'md_cjk': cjk_count(text),
            'modes': sorted({pg.get('source_mode') for pg in pages if pg.get('source_mode')}),
        })
    else:
        item['error'] = f"{r['error_code']}: {r['error_message']}"
    rows.append(item)

by_ext = {}
for it in rows:
    s = by_ext.setdefault(it['ext'], {'n': 0, 'done': 0, 'failed': 0, 'walls': [], 'pages': 0, 'md_cjk': []})
    s['n'] += 1
    if it['status'] == 'done':
        s['done'] += 1
        s['walls'].append(it['wall_s'])
        s['pages'] += it['pages']
        s['md_cjk'].append(it['md_cjk'])
    else:
        s['failed'] += 1
summary = {}
for ext, s in by_ext.items():
    ws = sorted(s['walls'])
    summary[ext] = {
        'n': s['n'], 'done': s['done'], 'failed': s['failed'],
        'pages_total': s['pages'],
        'wall_p50_s': ws[len(ws) // 2] if ws else None,
        'wall_max_s': ws[-1] if ws else None,
        'md_cjk_min': min(s['md_cjk']) if s['md_cjk'] else None,
    }
report = {'submit_errs': errs, 'total_wall_s': round(wall, 2), 'summary': summary, 'items': rows}
OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2))
print(json.dumps({'total_wall_s': round(wall, 2), 'submit_errs': errs, 'summary': summary},
                 ensure_ascii=False, indent=2))
print('\nFAILURES:')
for it in rows:
    if it['status'] != 'done':
        print(' ', it['file'], it['error'])
print('\nLOW-CONTENT (<100 CJK chars):')
for it in rows:
    if it['status'] == 'done' and it.get('md_cjk', 0) < 100:
        print(' ', it['file'], it.get('md_cjk'), 'cjk, pages', it.get('pages'), it.get('modes'))
