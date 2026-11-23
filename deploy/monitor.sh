#!/bin/bash
# 资源监控:每秒采样 GPU利用率/显存 + cgroup CPU用量,共 N 秒后自终止
N=${1:-300}; OUT=${2:-/tmp/mon_t2.log}; : > "$OUT"
for i in $(seq 1 "$N"); do
  TS=$(date +%s)
  GPU=$(nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader,nounits | head -1 | tr -d ' ')
  CPU=$(awk '/usage_usec/{print $2}' /sys/fs/cgroup/cpu.stat)
  echo "$TS $GPU $CPU" >> "$OUT"
  sleep 1
done
echo "MON_DONE" >> "$OUT"
