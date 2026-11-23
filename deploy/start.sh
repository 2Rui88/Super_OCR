#!/bin/bash
# OCR 服务拓扑启动脚本(4090D 22核 调优配置)
# 用法: bash start.sh                    → 默认 4 worker(gpu-1..4)
#       WORKERS='gpu-1 gpu-2' bash start.sh → 2 worker(A/B 轮 A)
export OCR_DEVICE=gpu:0 OCR_STRUCTURE_EAGER=1
export OCR_API_HOST=0.0.0.0
export OCR_CARD_CPU_THREADS=4 OCR_DOCUMENT_CPU_THREADS=4
export OCR_MAX_RUNNING_DOCUMENTS=3
export OCR_CARD_RETRY_SIDE=1280
export OMP_NUM_THREADS=4
# 证件配置试跑的测试集来源(需含 标准答案.json)
export OCR_TRIAL_DIRS="公开样例=/root/rivermind-data/01-OCR流水线/资料/公开样例;自造测试集=/root/rivermind-data/01-OCR流水线/资料/自造测试集;OCR测试集50张=/root/rivermind-data/OCR测试集/01_证件图片"
# Office 转换临时目录必须与 uploads/converted 同文件系统(doc_convert 同盘原子改名),
# /tmp 是系统盘而数据在 /root/rivermind-data → 跨盘 rename 会 EXDEV
export TMPDIR=/root/rivermind-data/tmp
mkdir -p "$TMPDIR"
WORKERS="${WORKERS:-gpu-1 gpu-2 gpu-3 gpu-4}"
cd /root/rivermind-data/01-OCR流水线/服务 || exit 1
nohup .venv/bin/python -m uvicorn app.api.app:app --host 0.0.0.0 --port 8700 > /root/rivermind-data/logs/api.log 2>&1 < /dev/null &
sleep 3
for W in $WORKERS; do
  nohup .venv/bin/python -m app.worker --worker-id $W > /root/rivermind-data/logs/w-$W.log 2>&1 < /dev/null &
  sleep 2
done
echo "SERVICES_STARTED workers=$WORKERS"
