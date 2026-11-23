#!/bin/bash
# 仅重启 P1 API 进程(worker 不动)——改了 API 层代码后用这个
export OCR_DEVICE=gpu:0 OCR_STRUCTURE_EAGER=1
export OCR_API_HOST=0.0.0.0
export OCR_CARD_CPU_THREADS=4 OCR_DOCUMENT_CPU_THREADS=4
export OCR_MAX_RUNNING_DOCUMENTS=3
export OCR_CARD_RETRY_SIDE=1280
export OMP_NUM_THREADS=4
export TMPDIR=/root/rivermind-data/tmp
export OCR_TRIAL_DIRS="公开样例=/root/rivermind-data/01-OCR流水线/资料/公开样例;自造测试集=/root/rivermind-data/01-OCR流水线/资料/自造测试集;OCR测试集50张=/root/rivermind-data/OCR测试集/01_证件图片"
PID=$(ps aux | grep 'uvicorn app[.]api[.]app:app --host 0[.]0[.]0[.]0 --port 8700' | grep -v grep | awk '{print $2}')
if [ -n "$PID" ]; then
  kill $PID
  sleep 3
  kill -9 $PID 2>/dev/null
fi
cd /root/rivermind-data/01-OCR流水线/服务 || exit 1
nohup .venv/bin/python -m uvicorn app.api.app:app --host 0.0.0.0 --port 8700 > /root/rivermind-data/logs/api.log 2>&1 < /dev/null &
sleep 4
echo "API_RESTARTED pid=$(ps aux | grep 'uvicorn app[.]api[.]app:app --host 0[.]0[.]0[.]0 --port 8700' | grep -v grep | awk '{print $2}') workers_untouched=$(ps aux | grep 'python -m app[.]worker' | grep -v grep | wc -l) p2_untouched=$(ps aux | grep 'uvicorn app[.]api[.]app:app --host 127[.]0[.]0[.]1 --port 8800' | grep -v grep | wc -l)"
