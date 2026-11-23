"""摄入消费循环 —— 进程内后台任务，由 API lifespan 启动。

模型与定稿设计一致：每 30s 顺带一次超时回收（幂等，无启动清扫）；
先占槽再领单；每份文档由「执行 + 心跳」双协程护航——心跳先睡后跳，
仅持有者可续；发现租约丢失（回收/接管/管理端重置）立即取消执行止损。

实现纪律：LibreOffice 转换、分块入库走线程池；调 P1 用异步 HTTP——
事件循环不被阻塞，问答 SSE 才能共存（自测项：长文档处理中问答仍流式）。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import time

from app import pipeline, queue
from app.config import settings

log = logging.getLogger("kb.worker")

_worker_task: asyncio.Task | None = None
_inflight: set[asyncio.Task] = set()
_worker_id: str | None = None


def worker_id() -> str:
    """惰性计算：避免 preload 场景 fork 前取到 master 的 pid。"""
    global _worker_id
    if _worker_id is None:
        _worker_id = f"{socket.gethostname()}:{os.getpid()}"
    return _worker_id


async def _heartbeat(job, lease_lost: asyncio.Event) -> None:
    """任务租约心跳：先睡后跳（短任务一生零心跳，领单写入即第 0 跳）。

    renewed → 续约成功，抖动计数清零；finished → 自己刚收尾，安静退出；
    lost / 连续 N 次续不上 → 置事件，交由 _run_job 取消执行止损。
    """
    misses = 0
    while True:
        await asyncio.sleep(settings.heartbeat_seconds)
        try:
            state = await asyncio.to_thread(queue.renew_lease, job.id, job.lease_token)
        except Exception:
            misses += 1
            if misses >= settings.lease_miss_tolerance:
                log.warning("心跳连续 %d 次失败：doc=%s（按丢租约止损）",
                            misses, job.id)
                lease_lost.set()
                return
            continue  # 偶发 DB 抖动容忍，不误杀
        if state == queue.LEASE_RENEWED:
            misses = 0
            continue
        if state == queue.LEASE_FINISHED:
            return
        log.warning("租约丢失：doc=%s worker=%s（行已被回收/接管/重置）",
                    job.id, job.worker)
        lease_lost.set()
        return


async def _process(job) -> tuple[bool, str]:
    return await pipeline.run_ingest(job)


async def _run_job(job, slots: asyncio.Semaphore) -> None:
    lease_lost = asyncio.Event()
    heartbeat = asyncio.create_task(_heartbeat(job, lease_lost))
    execute = asyncio.create_task(_process(job))
    watcher = asyncio.create_task(lease_lost.wait())
    try:
        await asyncio.wait({execute, watcher}, return_when=asyncio.FIRST_COMPLETED)
        if lease_lost.is_set() and not execute.done():
            execute.cancel()  # 止损：行已易主，继续跑只会重复烧 OCR/API
            log.warning("已取消止损：doc=%s worker=%s", job.id, job.worker)
        await asyncio.gather(execute, return_exceptions=True)
    except asyncio.CancelledError:
        execute.cancel()
        try:
            await asyncio.to_thread(queue.release_lease, job.id, job.lease_token)
        except Exception:
            log.exception("归还任务失败，将由超时回收处理：doc=%s", job.id)
        raise
    finally:
        heartbeat.cancel()
        watcher.cancel()
        try:
            await asyncio.gather(execute, watcher, heartbeat, return_exceptions=True)
        finally:
            slots.release()


async def _run_loop() -> None:
    slots = asyncio.Semaphore(max(1, settings.ingest_concurrency))
    next_reclaim = 0.0
    log.info("摄入 worker 启动：%s 并发=%d", worker_id(), settings.ingest_concurrency)
    while True:
        try:
            now = time.monotonic()
            if now >= next_reclaim:
                reclaimed = await asyncio.to_thread(queue.reclaim_stale)
                if reclaimed:
                    log.warning("回收 %d 个无心跳任务", reclaimed)
                next_reclaim = now + settings.reclaim_interval_seconds
            await slots.acquire()
            claim = asyncio.create_task(asyncio.to_thread(queue.claim_next, worker_id()))
            try:
                job = await asyncio.shield(claim)
            except asyncio.CancelledError:
                # SQLite 领单可能已在线程里提交。停机时取回结果并归还。
                try:
                    job = await claim
                    if job is not None:
                        await asyncio.to_thread(queue.release_lease, job.id, job.lease_token)
                except Exception:
                    log.exception("停机时领单或归还失败，将由超时回收处理")
                finally:
                    slots.release()
                raise
            except Exception:
                slots.release()
                raise
            if job is None:
                slots.release()
                await asyncio.sleep(2)
                continue
            log.info("领单 doc=%d %s", job.id, job.filename)
            task = asyncio.create_task(_run_job(job, slots))
            _inflight.add(task)
            task.add_done_callback(_inflight.discard)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("消费循环异常")
            await asyncio.sleep(2)


def start_worker() -> None:
    """由 API lifespan 调用：启动本进程消费循环（幂等）。"""
    global _worker_task
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_run_loop())


async def stop_worker() -> None:
    """先停止领单，给予在途任务收尾时间，再取消并归还未完成的任务。"""
    global _worker_task
    if _worker_task is None:
        return
    _worker_task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await _worker_task
    _worker_task = None
    if _inflight:
        _, pending = await asyncio.wait(_inflight, timeout=settings.drain_timeout_seconds)
        if pending:
            log.info("停机归还 %d 个在途任务", len(pending))
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        _inflight.clear()
