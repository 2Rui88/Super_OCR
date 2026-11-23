#!/bin/bash
# P2 制度知识库启动(CPU 限 4 线,仅本机+隧道可达)
export OMP_NUM_THREADS=4
cd /root/rivermind-data/02-制度知识库/服务 || exit 1
nohup .venv/bin/python -m uvicorn app.api.app:app --host 127.0.0.1 --port 8800 > /root/rivermind-data/logs/p2.log 2>&1 < /dev/null &
echo "P2_STARTED pid=$!"
