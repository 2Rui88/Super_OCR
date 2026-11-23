#!/bin/bash
# 串行下载 paddle GPU wheel 的 15 个 nvidia 依赖(阿里源,PK 魔数校验,断点续传)
set -u
H=https://mirrors.aliyun.com/pypi
WORK=/tmp/nvdl
mkdir -p "$WORK"

python - <<'PYEOF' > "$WORK/urls.txt"
import urllib.request, re
H = 'https://mirrors.aliyun.com/pypi'
pkgs = [
    ('nvidia-cuda-nvrtc-cu12', '12.6.77'),
    ('nvidia-cuda-runtime-cu12', '12.6.77'),
    ('nvidia-cuda-cupti-cu12', '12.6.80'),
    ('nvidia-cudnn-cu12', '9.5.1.17'),
    ('nvidia-cublas-cu12', '12.6.4.1'),
    ('nvidia-cufft-cu12', '11.3.0.4'),
    ('nvidia-curand-cu12', '10.3.7.77'),
    ('nvidia-cusolver-cu12', '11.7.1.2'),
    ('nvidia-cusparse-cu12', '12.5.4.2'),
    ('nvidia-cusparselt-cu12', '0.6.3'),
    ('nvidia-nccl-cu12', '2.25.1'),
    ('nvidia-nvtx-cu12', '12.6.77'),
    ('nvidia-nvjitlink-cu12', '12.6.85'),
    ('nvidia-cufile-cu12', '1.11.1.6'),
    ('nvidia-cuda-cccl-cu12', '12.6.77'),
]
for name, ver in pkgs:
    stem = name.replace('-', '_') + '-' + ver + '-'
    html = urllib.request.urlopen(f'{H}/simple/{name}/', timeout=30).read().decode()
    links = [l.split('#')[0] for l in re.findall(r'href=["\']?([^"\' >]+)', html)]
    cand = [u for u in links if stem in u and 'x86_64' in u and u.endswith('.whl')]
    if not cand:
        print(f'NOHREF {name}')
        continue
    u = cand[-1]
    if u.startswith('../../'):
        u = H + '/' + u[6:]
    elif u.startswith('/'):
        u = 'https://mirrors.aliyun.com' + u
    print(u)
PYEOF

FAIL=0
N=0
while read -r url; do
    N=$((N+1))
    case "$url" in
        NOHREF*) echo "FAIL_RESOLVE $url"; FAIL=1; continue;;
    esac
    fn=$(basename "$url")
    [ -f "$WORK/$fn" ] && head -c 2 "$WORK/$fn" | grep -q PK && { echo "SKIP $fn"; continue; }
    ok=0
    for try in 1 2 3 4 5; do
        curl -sS -C - --max-time 240 -o "$WORK/part.whl" "$url"
        magic=$(head -c 2 "$WORK/part.whl" 2>/dev/null)
        size=$(stat -c %s "$WORK/part.whl" 2>/dev/null || echo 0)
        if [ "$magic" = "PK" ] && [ "$size" -gt 100000 ]; then
            mv -f "$WORK/part.whl" "$WORK/$fn"
            echo "OK $fn $((size/1024/1024))MB"
            ok=1
            break
        fi
        echo "  retry$try magic=$magic size=$size"
        sleep 2
    done
    [ $ok -eq 0 ] && { echo "FAIL $url"; rm -f "$WORK/part.whl"; FAIL=1; }
done < "$WORK/urls.txt"

echo "TOTAL_OK $(ls "$WORK"/*.whl 2>/dev/null | wc -l)/15"
echo "NVDL_DONE_$FAIL"
