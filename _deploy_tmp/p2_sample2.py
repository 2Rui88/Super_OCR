"""P2→P1 资源采样 v2:状态直读 kb.sqlite3,窗口=提交→ready。"""
import json
import sqlite3
import subprocess
import time
import urllib.request
from pathlib import Path

P2 = 'http://127.0.0.1:8800/api/v1'
SRC = Path('/root/rivermind-data/01-OCR流水线/资料/扫描文档/质量手册 QM-2025（扫描）.pdf')
KB = Path('/root/rivermind-data/02-制度知识库/服务/data/kb.sqlite3')


def cpu_us():
    for line in open('/sys/fs/cgroup/cpu.stat'):
        k, v = line.split()
        if k == 'usage_usec':
            return int(v)
    return 0


def gpu_mem():
    out = subprocess.check_output(
        ['nvidia-smi', '--query-gpu=memory.used', '--format=csv,noheader,nounits'],
        text=True).strip()
    return int(out.split(',')[0])


up = SRC.read_bytes() + f'\n%sample2-{time.time()}\n'.encode()
boundary = '----kbdeploy2'
body = (f'--{boundary}\r\nContent-Disposition: form-data; name="files";'
        f' filename="sample2_质量手册.pdf"\r\nContent-Type: application/pdf\r\n\r\n').encode() \
    + up + f'\r\n--{boundary}--\r\n'.encode()

cpu0, mem0, t0 = cpu_us(), gpu_mem(), time.perf_counter()
req = urllib.request.Request(
    P2 + '/documents', data=body, method='POST',
    headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
doc_id = json.loads(urllib.request.urlopen(req, timeout=60).read())['results'][0]['doc_id']

peak_mem = mem0
while time.perf_counter() - t0 < 240:
    db = sqlite3.connect(KB)
    row = db.execute('select status, page_count from documents where id=?',
                     (doc_id,)).fetchone()
    db.close()
    peak_mem = max(peak_mem, gpu_mem())
    if row and row[0] in ('ready', 'failed'):
        break
    time.sleep(0.5)

wall = time.perf_counter() - t0
cpu_delta_s = (cpu_us() - cpu0) / 1e6
print(json.dumps({
    'doc_id': doc_id, 'status': row[0], 'pages': row[1],
    'wall_s': round(wall, 1),
    'cpu_core_seconds': round(cpu_delta_s, 2),
    'cpu_avg_cores': round(cpu_delta_s / wall, 2),
    'gpu_mem_before_mib': mem0, 'gpu_mem_peak_mib': peak_mem,
    'gpu_mem_delta_mib': peak_mem - mem0,
}, ensure_ascii=False))
