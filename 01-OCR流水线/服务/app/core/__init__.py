"""core 包对外直调入口 —— 供工具/实验脚本使用；服务运行走 app.worker。

历史说明：曾经的 CardService / CardProcessPoolService（常驻服务 + 进程池 +
自带缓存的旧架构）已随 worker 化架构下线，由 app.worker 的 ResultCache 与
平级 worker 进程取代。
"""
from app.core.document_pipeline import process_document

__all__ = ["process_document"]
