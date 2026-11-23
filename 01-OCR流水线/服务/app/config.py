"""服务运行配置 —— 唯一允许读环境变量的地方，业务代码只 import Settings。

目录约定（与 OCR 结果缓存分开，互不影响）：
- data/tasks.sqlite3  任务库（队列 + 状态 + 结果）
- uploads/            上传文件，按内容哈希命名，天然去重
- .cache/cards.sqlite3 OCR 结果缓存（原有，worker 只增不改其表结构）
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _parse_trial_dirs(raw: str) -> tuple[tuple[str, str], ...]:
    """OCR_TRIAL_DIRS="名称=路径;名称=路径" → ((名称, 路径), ...)。"""
    entries = []
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        label, _, path = chunk.partition("=")
        label, path = label.strip(), path.strip()
        if label and path:
            entries.append((label, path))
    return tuple(entries)


@dataclass(frozen=True)
class Settings:
    task_db_path: Path = ROOT / "data" / "tasks.sqlite3"
    cache_db_path: Path = ROOT / ".cache" / "cards.sqlite3"
    upload_dir: Path = ROOT / "uploads"
    cert_dir: Path = ROOT / "config" / "certs"

    # 同步接口：POST /cards 最多等多久，超时转 202 + task_id
    sync_wait_seconds: float = 60.0
    # worker 轮询间隔与心跳节奏；心跳超过 stale 秒视为 worker 失联
    task_poll_seconds: float = 1.0
    heartbeat_seconds: float = 5.0
    worker_stale_seconds: float = 30.0
    # 扫描文档任务全局并发上限（PP-StructureV3 重，默认保守 1；大内存机器可调高）
    max_running_documents: int = 1

    # 上传限制：扩展名白名单在 API 层按接口另行约束，这里只限大小
    max_upload_mb: int = 20

    # 引擎 CPU 线程（文档引擎全局并发为 1，可适当调高）
    card_cpu_threads: int = 4
    document_cpu_threads: int = 4

    # 推理设备："cpu"（默认）| "gpu" / "gpu:0"。进引擎与缓存签名，切换后缓存自动分离
    device: str = "cpu"
    # worker 启动时是否预加载 PP-StructureV3（GPU 部署建议开，免掉首个任务的加载毛刺）
    structure_eager_load: bool = False
    # 证件低置信/校验失败时的检测长边重试档（如 1280）；0 = 关闭重试。
    # 依赖 PaddleOCR predict() 运行时参数，启用前需在目标环境证伪验证
    card_retry_side: int = 0

    # Office → PDF 转换（LibreOffice headless）
    soffice_bin: str = "soffice"            # Windows 本地开发可指 C:/Program Files/LibreOffice/program/soffice.exe
    convert_timeout_seconds: float = 120.0  # 单文件转换超时（密码/损坏文件可能挂死）
    max_document_pages: int = 200           # 转换后页数上限（Excel 宽表分页可能爆炸）

    # 证件配置试跑的测试集来源：[(显示名, 目录)]，目录需含 标准答案.json + 图片。
    # 环境 OCR_TRIAL_DIRS="名称=路径;名称=路径" 覆盖；缺省用仓库内自造测试集
    trial_source_dirs: tuple[tuple[str, str], ...] = ()

    # API 监听（python -m app.api 使用）。默认 8700 与对接文档/项目二的
    # KB_P1_BASE_URL 默认值一致——直接 python -m app.api 起服务即可被项目二连通
    api_host: str = "127.0.0.1"
    api_port: int = 8700

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            task_db_path=Path(os.environ.get("OCR_TASK_DB", cls.task_db_path)),
            cache_db_path=Path(os.environ.get("OCR_CACHE_DB", cls.cache_db_path)),
            upload_dir=Path(os.environ.get("OCR_UPLOAD_DIR", cls.upload_dir)),
            sync_wait_seconds=_env_float("OCR_SYNC_WAIT_SECONDS", cls.sync_wait_seconds),
            task_poll_seconds=_env_float("OCR_TASK_POLL_SECONDS", cls.task_poll_seconds),
            heartbeat_seconds=_env_float("OCR_HEARTBEAT_SECONDS", cls.heartbeat_seconds),
            worker_stale_seconds=_env_float("OCR_WORKER_STALE_SECONDS", cls.worker_stale_seconds),
            max_running_documents=_env_int("OCR_MAX_RUNNING_DOCUMENTS",
                                           cls.max_running_documents),
            max_upload_mb=_env_int("OCR_MAX_UPLOAD_MB", cls.max_upload_mb),
            card_cpu_threads=_env_int("OCR_CARD_CPU_THREADS", cls.card_cpu_threads),
            document_cpu_threads=_env_int("OCR_DOCUMENT_CPU_THREADS", cls.document_cpu_threads),
            device=os.environ.get("OCR_DEVICE", cls.device),
            structure_eager_load=_env_bool("OCR_STRUCTURE_EAGER", cls.structure_eager_load),
            card_retry_side=_env_int("OCR_CARD_RETRY_SIDE", cls.card_retry_side),
            soffice_bin=os.environ.get("OCR_SOFFICE_BIN", cls.soffice_bin),
            convert_timeout_seconds=_env_float("OCR_CONVERT_TIMEOUT_SECONDS",
                                               cls.convert_timeout_seconds),
            max_document_pages=_env_int("OCR_MAX_DOCUMENT_PAGES", cls.max_document_pages),
            api_host=os.environ.get("OCR_API_HOST", cls.api_host),
            api_port=_env_int("OCR_API_PORT", cls.api_port),
            trial_source_dirs=_parse_trial_dirs(os.environ.get(
                "OCR_TRIAL_DIRS",
                f"自造测试集={cls._default_trial_dir()}")),
        )

    @staticmethod
    def _default_trial_dir() -> str:
        return str((ROOT.parent / "资料" / "自造测试集"))

    def __post_init__(self):
        if self.device != "cpu" and not self.device.startswith("gpu"):
            raise ValueError("OCR_DEVICE must be 'cpu', 'gpu' or 'gpu:<idx>'")
        if self.card_retry_side < 0:
            raise ValueError("OCR_CARD_RETRY_SIDE must be >= 0")
        if self.max_document_pages < 1:
            raise ValueError("OCR_MAX_DOCUMENT_PAGES must be >= 1")
        if self.convert_timeout_seconds <= 0:
            raise ValueError("OCR_CONVERT_TIMEOUT_SECONDS must be > 0")


settings = Settings.from_env()
