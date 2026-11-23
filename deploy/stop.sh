#!/bin/bash
# 按 PID 安全停止 OCR 服务(API + workers),绝不 pkill -f
PATS='uvicorn app[.]api[.]app|python -m app[.]worker'
for pid in $(ps aux | grep -E "$PATS" | grep -v grep | awk '{print $2}'); do
  kill "$pid" 2>/dev/null
done
sleep 3
for pid in $(ps aux | grep -E "$PATS" | grep -v grep | awk '{print $2}'); do
  kill -9 "$pid" 2>/dev/null
done
LEFT=$(ps aux | grep -E "$PATS" | grep -v grep | wc -l)
echo "REMAINING_$LEFT"
