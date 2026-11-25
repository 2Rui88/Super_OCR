"""P2→P1 真实负载资源采样:唯一字节 PDF 强制全链路 OCR,记录 CPU/显存增量。"""
import json
import subprocess
import time
import urllib.request
from pathlib import Path

P2 = 'http://127.0.0.1:8800/api/v1'
SRC = Path('/root/rivermind-data/01-OCR流水线/资料/扫描文档/印章管理规定2021（盖章扫描件）.pdf')
WORK = Path('/tmp/p2_sample'); WORK.mkdir(exist_ok=True)


def cpu_usage_us():
    for line in open('/sys/fs/cgroup/cpu.stat'):
        k, v = line.split()
        if k == 'usage_usec':
            return int(v)
    return 0


def gpu():
    out = subprocess.check_output(
        ['nvidia-smi', '--query-gpu=utilization.gpu,memory.used',
         '--format=csv,noheader,nounits'], text=True).strip()
    util, mem = (int(x) for x in out.split(','))
    return util, mem


up = SRC.read_bytes() + f'\n%sample-{time.time()}\n'.encode()
f = WORK / 'sample.pdf'
f.write_bytes(up)

cpu0 = cpu_usage_us()
_, mem0 = gpu()
t0 = time.perf_counter()

boundary = '----kbdeploy'
body = (f'--{boundary}\r\nContent-Disposition: form-data; name="files";'
        f' filename="sample_印章.pdf"\r\nContent-Type: application/pdf\r\n\r\n').encode() \
    + up + f'\r\n--{boundary}--\r\n'.encode()
req = urllib.request.Request(
    P2 + '/documents', data=body, method='POST',
    headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
doc_id = json.loads(urllib.request.urlopen(req, timeout=60).read())['results'][0]['doc_id']
print('doc_id:', doc_id, flush=True)

peak_util, peak_mem = 0, mem0
while time.perf_counter() - t0 < 300:
    d = json.loads(urllib.request.urlopen(f'{P2}/documents/{doc_id}', timeout=30).read())
    util, mem = gpu()
    peak_util, peak_mem = max(peak_util, util), max(peak_mem, mem)
    if d.get('status') in ('ready', 'failed'):
        break
    time.sleep(0.5)

wall = time.perf_counter() - t0
cpu1 = cpu_usage_us()
print(json.dumps({
    'doc_id': doc_id, 'status': d.get('status'),
    'pages': d.get('page_count'), 'wall_s': round(wall, 1),
    'cpu_core_seconds_delta': round((cpu1 - cpu0) / 1e6, 2),
    'cpu_avg_cores_during': round((cpu1 - cpu0) / 1e6 / wall, 2),
    'gpu_util_peak_pct': peak_util,
    'gpu_mem_before_mib': mem0, 'gpu_mem_peak_mib': peak_mem,
}, ensure_ascii=False))
