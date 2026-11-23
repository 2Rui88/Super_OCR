"""队列水位 —— 可观察性（堵"进程活着但没人消费"的盲区）。"""
from __future__ import annotations

from fastapi import APIRouter

from app import queue

router = APIRouter(tags=["queue"])


@router.get("/queue/stats")
def queue_stats():
    return queue.stats()
