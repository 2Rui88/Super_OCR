"""对比新测试集与服务器自造测试集的内容异同 + 检查 golden 覆盖。"""
import hashlib
import json
from pathlib import Path

BASE = Path('/root/rivermind-data')
new = BASE / 'OCR测试集/01_证件图片'
old = BASE / '01-OCR流水线/资料/自造测试集'
gt = json.load(open(new / '标准答案.json', encoding='utf-8'))
print('golden entries:', len(gt))
print('golden keys sample:', list(gt[0].keys()))
print('invalid_fields total:', sum(len(g.get('invalid_fields') or []) for g in gt))


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None


same, diff, miss = [], [], []
for g in gt:
    n, o = new / g['file'], old / g['file']
    if not n.exists():
        miss.append(('new_missing', g['file']))
        continue
    if not o.exists():
        miss.append(('old_missing', g['file']))
        continue
    (same if sha(n) == sha(o) else diff).append(g['file'])
print(f'same_content={len(same)} diff_content={len(diff)} no_counterpart={len(miss)}')
for m in miss[:10]:
    print(' ', m)
for d in diff[:10]:
    print('  DIFF:', d)
