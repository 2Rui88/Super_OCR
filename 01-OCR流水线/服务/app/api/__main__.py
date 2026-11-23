"""开发启动入口：python -m app.api。生产部署：uvicorn app.api.app:app --host 0.0.0.0 --port 8700。"""
from app.config import settings

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.api.app:app", host=settings.api_host, port=settings.api_port)
