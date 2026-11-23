# 本地开发启动：API + 双 worker（生产用 docker compose up -d）
# 在 服务 目录执行：powershell -File scripts\start_dev.ps1
$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"
$py = ".\.venv\Scripts\python.exe"

Start-Process $py -ArgumentList "-m", "uvicorn", "app.api.app:app", "--host", "127.0.0.1", "--port", "8700"
Start-Process $py -ArgumentList "-m", "app.worker", "--worker-id", "worker-1"
Start-Process $py -ArgumentList "-m", "app.worker", "--worker-id", "worker-2"

Write-Host "API     http://127.0.0.1:8700/api/v1/health  (前端 http://127.0.0.1:8700/)"
Write-Host "worker-1 / worker-2 已启动"
