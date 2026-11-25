"""诊断:对照标准答案,列出 7 个故意做错字段的检出状态与置信度。"""
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path('/root/rivermind-data/01-OCR流水线')
gt = json.load(open(ROOT / '资料/自造测试集/标准答案.json', encoding='utf-8'))
conn = sqlite3.connect(ROOT / '服务/data/tasks.sqlite3')
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "select t.filename, t.status, t.result_json from tasks t "
    "join batch_items bi on bi.task_id = t.id "
    "where bi.batch_id = (select max(id) from batches) and t.task_type='card'").fetchall()
by_file = {r['filename']: r for r in rows}
print(f"tasks={len(rows)}")
for g in gt:
    inv = g.get('invalid_fields') or []
    if not inv:
        continue
    r = by_file.get(g['file'])
    if r is None or r['status'] != 'done':
        print(f"[PENDING] {g['file']}")
        continue
    env = json.loads(r['result_json'])
    fmap = {f['name']: f for f in env['result'].get('fields', [])}
    for fname in inv:
        f = fmap.get(fname)
        if f is None:
            print(f"[MISS-FIELD] {g['file']} {fname} 字段缺失")
            continue
        mark = 'HIT ' if f.get('needs_review') else 'MISS'
        print(f"[{mark}] {g['file']:<28} {fname:<8} conf={f.get('confidence'):.4f} "
              f"review={f.get('needs_review')} "
              f"val_errors={[e.get('code', e) if isinstance(e, dict) else e for e in f.get('validation', {}).get('errors', [])]} "
              f"value={str(f.get('value'))[:20]}")
