"""OCR 任务 worker —— 平级进程，一次只处理一个任务。

启动：python -m app.worker --worker-id worker-1（worker-2 同理）

职责与约定：
- 所有文档统一逐页走 PP-StructureV3（不再区分文字层/扫描），因此 worker
  只有一种形态：证件 + 文档双引擎全量装配；
- Office 文件先经 LibreOffice 转 PDF（产物按内容哈希复用）再识别；
- 文档结果按 内容哈希+引擎签名 整体缓存，重复提交秒回（全走模型后识别
  成本高，缓存从"图片专属优化"变为"文档线必需配套"）；
- 启动时先恢复自己上次遗留的 running 任务（标 SERVICE_RESTARTED），再加载证件模型；
- PP-StructureV3 默认懒加载；OCR_STRUCTURE_EAGER=1 时启动即预加载（GPU 部署建议开）；
- 推理设备由 OCR_DEVICE 控制（默认 cpu；gpu 部署设 gpu:0），进缓存签名自动分离；
- 心跳由独立线程持续上报（含模型加载标志与当前任务），失联由其他 worker 收割；
- 扫描文档任务全局并发 1 由任务库领取 SQL 保证，worker 内不再建任何线程/进程池。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import signal
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings, settings
from app.core.document_pipeline import (PaddleDocumentEngine, document_engine_signature,
                                        process_document)
from app.core.doc_convert import ConvertError, ensure_pdf, is_office_document
from app.core.card_pipeline import PIPELINE_VERSION
from app.core.image_routing import process_image
from app.core.task_store import ClaimedTask, TaskStore

log = logging.getLogger("app.worker")


@contextmanager
def _sqlite_connection(db_path: Path):
    """WAL 连接，用完必关（with sqlite3.Connection 只管事务不关句柄）。"""
    db = sqlite3.connect(db_path, timeout=10)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=10000")
        with db:
            yield db
    finally:
        db.close()


class ResultCache:
    """结果缓存（图片路由信封 + 文档信封）：键覆盖引擎签名 + 内容哈希。

    表名 image_results 是历史名（原本只缓存图片路由），文档信封共用一张表，
    键结构不同天然不冲突。
    """

    def __init__(self, db_path: Path | None):
        self.db_path = Path(db_path) if db_path else None
        if self.db_path:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as db:
                db.execute("CREATE TABLE IF NOT EXISTS image_results "
                           "(cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL)")

    def _connect(self):
        return _sqlite_connection(self.db_path)

    def get(self, key: str) -> str | None:
        if not self.db_path:
            return None
        try:
            with self._connect() as db:
                row = db.execute("SELECT payload FROM image_results WHERE cache_key=?",
                                 (key,)).fetchone()
            return row[0] if row else None
        except (sqlite3.Error, ValueError):
            log.warning("忽略无法读取的路由缓存条目")
            return None

    def put(self, key: str, payload: str) -> None:
        if not self.db_path:
            return
        try:
            with self._connect() as db:
                db.execute("INSERT OR REPLACE INTO image_results VALUES (?,?)", (key, payload))
        except sqlite3.Error:
            log.warning("路由缓存写入失败", exc_info=True)


class LazyDocumentReader:
    """PP-StructureV3 懒加载代理：跨任务常驻，退出时统一关闭。"""

    def __init__(self, factory):
        self._factory = factory
        self._engine = None

    @property
    def loaded(self) -> bool:
        return self._engine is not None

    def warm(self) -> None:
        """启动时预加载（急加载），首个任务不再承担模型初始化耗时。
        构造引擎对象后还要触发引擎自身的 warm()——引擎内部也是懒加载。"""
        if self._engine is None:
            self._engine = self._factory()
        engine_warm = getattr(self._engine, "warm", None)
        if callable(engine_warm):
            engine_warm()

    def __call__(self, image, page_num):
        if self._engine is None:
            self._engine = self._factory()
        return self._engine(image, page_num)

    def close(self):
        if self._engine is not None:
            self._engine.close()
            self._engine = None


class WorkerState:
    """主循环与心跳线程共享的可变状态。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.current_task_id: int | None = None

    def set_current(self, task_id: int | None) -> None:
        with self._lock:
            self.current_task_id = task_id

    def current(self) -> int | None:
        with self._lock:
            return self.current_task_id


class RegistryHolder:
    """生效证件 registry 容器：心跳发现整套指纹变化时原子整体替换。

    registry 与指纹必须成对切换（指纹进证件缓存键），读侧拿到的永远是
    同一套的一致快照。"""
    def __init__(self, registry: dict, set_hash: str):
        self._lock = threading.Lock()
        self._registry = registry
        self._hash = set_hash

    @property
    def registry(self) -> dict:
        with self._lock:
            return self._registry

    @property
    def set_hash(self) -> str:
        with self._lock:
            return self._hash

    def swap(self, registry: dict, set_hash: str) -> None:
        with self._lock:
            self._registry, self._hash = registry, set_hash


def _envelope_engine_type(envelope: dict) -> str:
    return "card_ocr" if envelope["kind"] == "card" else "structure_v3"


def _document_engine_type(result_dict: dict) -> str:
    """统一 StructureV3 后文档引擎恒定；保留函数以便将来区分转换/识别阶段。"""
    return "structure_v3"


class TrialNotReady(Exception):
    """试跑任务尚查不到归属（建行/挂批次与领取之间的窄窗口），应重排队而非失败。"""


def main() -> int:
    parser = argparse.ArgumentParser(description="OCR 任务 worker")
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--role", choices=("gpu", "cpu"), default="gpu",
                        help="兼容保留：统一 StructureV3 后 cpu 角色已下线，"
                             "传入仅避免旧启动脚本报错")
    parser.add_argument("--config", help="兼容保留：暂无额外配置文件需求")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    config: Settings = settings
    store = TaskStore(config.task_db_path)
    recovered = store.recover_worker_tasks(
        args.worker_id, error_code="SERVICE_RESTARTED",
        error_message="worker 重启前中断，请重新提交或重试")
    if recovered:
        log.warning("已恢复自己遗留的 %d 个 running 任务（标 SERVICE_RESTARTED）", recovered)

    from app.certs import load_certs
    from app.certs.loader import load_certs_from_texts, registry_hash
    from app.engine.paddle_card import PaddleCardConfig, PaddleCardEngine

    # 双引擎全量装配；document_text_layer 仅为旧库遗留 queued 行的兼容领取；
    # cert_trial_card 是证件配置试跑任务（用试跑快照 registry，独立缓存空间）
    claimable = ["card", "cert_trial_card",
                 "document_scan", "document", "document_text_layer"]
    card_engine = PaddleCardEngine(config=PaddleCardConfig(
        cpu_threads=config.card_cpu_threads, device=config.device))
    # registry 真源在 DB（active 版本集）；DB 未种子时回退 config/certs 文件
    active_texts = store.active_cert_texts()
    registry = load_certs_from_texts(active_texts) if active_texts \
        else load_certs(config.cert_dir)
    registry_holder = RegistryHolder(registry, registry_hash(registry))
    card_signature = card_engine.cache_signature
    document_reader = LazyDocumentReader(
        lambda: PaddleDocumentEngine(cpu_threads=config.document_cpu_threads,
                                     device=config.device))
    if config.structure_eager_load:
        log.info("%s 预加载 PP-StructureV3（急加载）……", args.worker_id)
        document_reader.warm()
        log.info("%s PP-StructureV3 预加载完成", args.worker_id)

    state = WorkerState()
    cache = ResultCache(config.cache_db_path)
    document_signature = document_engine_signature(device=config.device)

    stop = threading.Event()

    def heartbeat_loop() -> None:
        while not stop.wait(config.heartbeat_seconds):
            try:
                store.heartbeat(args.worker_id, pid=os.getpid(),
                                card_model_loaded=card_engine is not None,
                                document_model_loaded=(document_reader.loaded
                                                       if document_reader else False),
                                current_task_id=state.current())
                _maybe_reload_registry()
                for lost in store.reap_stale_workers(config.worker_stale_seconds):
                    log.warning("worker %s 心跳过期，其任务已标 WORKER_LOST", lost)
            except sqlite3.Error:
                log.warning("心跳写入失败，稍后重试", exc_info=True)

    def _maybe_reload_registry() -> None:
        """证件配置热更新：cert_state 指纹变化 → 原子换 registry + 缓存键指纹。"""
        latest = store.get_cert_state_hash()
        if not latest or latest == registry_holder.set_hash:
            return
        try:
            new_registry = load_certs_from_texts(store.active_cert_texts())
            if registry_hash(new_registry) != latest:
                log.warning("active 版本集指纹与 cert_state 不一致，本次不切换")
                return
            registry_holder.swap(new_registry, latest)
            log.info("%s 证件配置已热更新（%d 类，指纹 %s…），后续识别用新配置",
                     args.worker_id, len(new_registry), latest[:12])
        except Exception:
            log.warning("证件配置热重建失败，沿用旧配置", exc_info=True)

    threading.Thread(target=heartbeat_loop, daemon=True, name="heartbeat").start()

    def request_stop(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    try:
        signal.signal(signal.SIGTERM, request_stop)
    except (OSError, ValueError):
        pass  # Windows 下 SIGTERM 绑定可能不允许

    def _card_retry_needed(envelope: dict) -> bool:
        """触发 960→1280 重试：类型未识别 / 字段缺失 / 置信度低 / 校验失败。"""
        if envelope.get("kind") != "card":
            return False
        result = envelope["result"]
        if result.get("doc_type") == "unknown":
            return True
        fields = result.get("fields", [])
        return any(not field["value"] or field["needs_review"] for field in fields)

    def _card_score(envelope: dict) -> tuple:
        """择优打分：通过校验的字段数 > 非空字段数 > 类型置信度。平分保守取首跑。"""
        result = envelope["result"]
        fields = result.get("fields", [])
        passed = sum(1 for f in fields
                     if f["value"] and f["validation"]["passed"])
        filled = sum(1 for f in fields if f["value"])
        return (passed, filled, result.get("type_confidence", 0.0))

    def _run_card_with_registry(task: ClaimedTask, registry: dict,
                                key_prefix: list) -> tuple[str, dict]:
        """证件全链路（识别→分类→抽段→校验→低置信重试→缓存）。

        key_prefix 含 registry 指纹 → 配置变更/试跑快照天然分缓存空间。
        """
        key = hashlib.sha256(json.dumps(
            [*key_prefix, PIPELINE_VERSION, card_signature,
             document_signature, task.file_sha256, task.hint_type],
            ensure_ascii=False).encode()).hexdigest()
        cached = cache.get(key)
        if cached is not None:
            envelope = json.loads(cached)
        else:
            # 摆正图与原件同目录、按内容哈希命名；字段 bbox 属于摆正图坐标系
            rect_path = Path(config.upload_dir) / f"{task.file_sha256}.rect.jpg"
            envelope = process_image(task.file_path, card_engine=card_engine,
                                     document_reader=document_reader, registry=registry,
                                     sha256=task.file_sha256, hint_type=task.hint_type,
                                     card_rectified_out=rect_path)
            if (config.card_retry_side >= 32 and envelope.get("kind") == "card"
                    and _card_retry_needed(envelope)):
                # 低置信重试：同一常驻引擎，运行时切检测长边。predict() 不支持
                # 该参数时此处抛错，任务按 WORKER_ERROR 落败——启用前先做证伪实验
                retry = process_image(task.file_path, card_engine=card_engine,
                                      document_reader=document_reader,
                                      registry=registry, sha256=task.file_sha256,
                                      hint_type=task.hint_type,
                                      card_det_side=config.card_retry_side,
                                      card_rectified_out=rect_path)
                if _card_score(retry) > _card_score(envelope):
                    retry["result"]["det_side"] = config.card_retry_side
                    envelope = retry
                    log.info("任务 #%d 低置信重试（%d）采纳重试结果", task.id,
                             config.card_retry_side)
            if rect_path.is_file() and envelope.get("kind") == "card":
                # 仅证件信封携带摆正图；兜底转文档的信封 bbox 来自 StructureV3
                # 原图坐标系,不适用
                envelope["result"]["rectified_file"] = rect_path.name
            cache.put(key, json.dumps(envelope, ensure_ascii=False))
        # 结果里显示用户原始文件名；缓存条目本身与文件名无关（内容寻址）
        envelope["result"]["filename"] = task.filename
        return _envelope_engine_type(envelope), envelope

    def run_card_task(task: ClaimedTask) -> tuple[str, dict]:
        return _run_card_with_registry(
            task, registry_holder.registry, ["cert1", registry_holder.set_hash])

    trial_registries: dict[tuple[int, str], tuple[dict, str]] = {}

    def run_trial_card_task(task: ClaimedTask) -> tuple[str, dict]:
        """试跑任务：按归属试跑的快照（draft/ref）整套 registry 走同一条链路。"""
        trial = store.cert_trial_by_task(task.id)
        if trial is None:
            # 建行与挂批次之间存在窄窗口；重排队等 setup 完成（防孤儿任务死循环）
            row = store.get_task(task.id)
            if row and row["attempts"] < 10:
                store.requeue_running_task(task.id)
                raise TrialNotReady(f"试跑任务 #{task.id} 归属尚未就绪，已重排队")
            raise RuntimeError(f"试跑任务 #{task.id} 找不到归属的 cert_trials 行")
        cache_key = (trial["id"], trial["role"])
        if cache_key not in trial_registries:
            snapshot = trial["draft_set_json"] if trial["role"] == "draft" \
                else trial["ref_set_json"]
            snapshot_registry = load_certs_from_texts(json.loads(snapshot))
            trial_registries[cache_key] = (snapshot_registry,
                                           registry_hash(snapshot_registry))
        snapshot_registry, snapshot_hash = trial_registries[cache_key]
        return _run_card_with_registry(
            task, snapshot_registry, ["cert_trial1", snapshot_hash])

    def run_document_task(task: ClaimedTask) -> tuple[str, dict]:
        # 文档级缓存：全走 StructureV3 后识别成本高，同内容重复提交直接命中
        key = hashlib.sha256(json.dumps(
            ["document", document_signature, task.file_sha256],
            ensure_ascii=False).encode()).hexdigest()
        cached = cache.get(key)
        if cached is not None:
            envelope = json.loads(cached)
            envelope["result"]["filename"] = task.filename
            return _document_engine_type(envelope["result"]), envelope

        if is_office_document(task.file_path):
            store.update_progress(task.id, "Office 转 PDF", 0.0)
            pdf_path, converted = ensure_pdf(
                task.file_path, Path(config.upload_dir) / "converted",
                soffice_bin=config.soffice_bin,
                timeout_seconds=config.convert_timeout_seconds,
                max_pages=config.max_document_pages)
            if converted:
                log.info("任务 #%d %s 已转 PDF：%s", task.id, task.filename, pdf_path.name)
        else:
            pdf_path = Path(task.file_path)

        # 逐页真实状态：开始前报总数，每页完成后追加该页元信息（块/表/耗时）
        import pymupdf
        with pymupdf.open(pdf_path) as probe:
            total_pages = probe.page_count
        done_pages: list[dict] = []
        store.update_progress(task.id, f"识别中 第0/{total_pages}页", 0.0,
                              pages_progress={"total": total_pages, "pages": done_pages})

        def on_page(page_result, total: int) -> None:
            tables = sum(1 for block in page_result.blocks
                         if block.type.value == "table")
            done_pages.append({
                "page": page_result.page_num,
                "blocks": len(page_result.blocks),
                "tables": tables,
                "duration_ms": page_result.duration_ms,
                "warning": "未提取到文字或表格" if not page_result.blocks else None,
            })
            store.update_progress(task.id, f"识别中 第{page_result.page_num}/{total}页",
                                  page_result.page_num / total if total else 1.0,
                                  pages_progress={"total": total, "pages": done_pages})

        result = process_document(pdf_path, scan_reader=document_reader,
                                  on_page=on_page)
        result_dict = result.model_dump(mode="json")
        # file_sha256 归因到上传原件（Office 转换件与原件哈希不同）
        result_dict["file_sha256"] = task.file_sha256
        result_dict["filename"] = task.filename
        envelope = {"kind": "document", "reason": "", "result": result_dict}
        cache.put(key, json.dumps(envelope, ensure_ascii=False))
        return _document_engine_type(result_dict), envelope

    log.info("%s 就绪（device=%s，证件+文档模型），可领 %s，开始轮询任务",
             args.worker_id, config.device, claimable)
    while not stop.is_set():
        task = store.claim_next_task(args.worker_id,
                                     config.max_running_documents,
                                     claimable_types=claimable)
        if task is None:
            stop.wait(config.task_poll_seconds)
            continue
        state.set_current(task.id)
        log.info("领取任务 #%d [%s] %s", task.id, task.task_type, task.filename)
        started = time.perf_counter()
        try:
            if task.task_type == "card":
                engine_type, envelope = run_card_task(task)
            elif task.task_type == "cert_trial_card":
                engine_type, envelope = run_trial_card_task(task)
            else:
                engine_type, envelope = run_document_task(task)
            store.finish_task(task.id,
                              result_json=json.dumps(envelope, ensure_ascii=False),
                              engine_type=engine_type)
            log.info("任务 #%d 完成（%s，%.1fs）", task.id, engine_type,
                     time.perf_counter() - started)
        except ConvertError as exc:
            # 转换失败是确定性的（损坏/加密/页数超限），重试无意义 → 永久失败
            store.fail_task(task.id, error_code=exc.error_code,
                            error_message=str(exc))
            log.warning("任务 #%d 转换失败（%s）：%s", task.id, exc.error_code, exc)
        except TrialNotReady as exc:
            # 任务已重排队，不算失败；立即回循环重新领取
            log.info("%s", exc)
        except Exception as exc:
            store.fail_task(task.id, error_code="WORKER_ERROR",
                            error_message=f"{type(exc).__name__}: {exc}")
            log.exception("任务 #%d 失败", task.id)
        finally:
            state.set_current(None)

    if document_reader is not None:
        document_reader.close()
    if card_engine is not None:
        card_engine.close()
    log.info("%s 已退出", args.worker_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
