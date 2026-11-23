"""配置 —— 全部支持环境变量覆盖，命名前缀 KB_。"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_dotenv() -> None:
    """读取 服务/.env（KEY=VALUE，# 注释）。不覆盖已有环境变量。

    .env 已在 .gitignore 中排除——密钥只落本文件，不进代码仓库（考核要求）。
    """
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


class Settings:
    """进程内单例读取（与项目一 config.py 同风格）。"""

    def __init__(self) -> None:
        self.host: str = os.environ.get("KB_API_HOST", "127.0.0.1")
        self.port: int = int(os.environ.get("KB_API_PORT", "8800"))

        # 存储布局：库真身 / 原件 / 转换 PDF / 流程图裁图
        self.data_dir: Path = Path(os.environ.get("KB_DATA_DIR", ROOT / "data"))
        self.upload_dir: Path = Path(os.environ.get("KB_UPLOAD_DIR", ROOT / "uploads"))
        self.crop_dir: Path = Path(os.environ.get("KB_CROP_DIR", ROOT / "data" / "crops"))
        self.db_path: Path = self.data_dir / "kb.sqlite3"

        # 项目一 OCR 服务
        self.p1_base_url: str = os.environ.get("KB_P1_BASE_URL", "http://127.0.0.1:8700/api/v1")
        self.p1_poll_start_seconds: float = float(os.environ.get("KB_P1_POLL_START", "2"))
        self.p1_poll_max_seconds: float = float(os.environ.get("KB_P1_POLL_MAX", "10"))
        # 轮询超时须覆盖 P1 上限文档的识别时长：CPU 上 StructureV3 约 10~15s/页，
        # P1 允许最多 200 页（OCR_MAX_DOCUMENT_PAGES）≈ 50 分钟，取 1 小时留余量
        self.p1_poll_timeout_seconds: float = float(os.environ.get("KB_P1_POLL_TIMEOUT", "3600"))
        self.p1_resubmit_max: int = int(os.environ.get("KB_P1_RESUBMIT_MAX", "2"))

        # 摄入队列（心跳租约）
        self.max_attempts: int = int(os.environ.get("KB_MAX_ATTEMPTS", "3"))
        self.lease_timeout_seconds: int = int(os.environ.get("KB_LEASE_TIMEOUT", "300"))
        self.heartbeat_seconds: float = float(os.environ.get("KB_HEARTBEAT_SECONDS", "30"))
        self.reclaim_interval_seconds: float = float(os.environ.get("KB_RECLAIM_INTERVAL", "30"))
        self.lease_miss_tolerance: int = int(os.environ.get("KB_LEASE_MISS_TOLERANCE", "3"))
        self.ingest_concurrency: int = int(os.environ.get("KB_INGEST_CONCURRENCY", "2"))
        self.drain_timeout_seconds: float = float(os.environ.get("KB_DRAIN_TIMEOUT", "5"))

        # 上传约束（与项目一一致）
        self.max_upload_mb: int = int(os.environ.get("KB_MAX_UPLOAD_MB", "20"))
        self.allowed_exts = {".doc", ".docx", ".pdf"}

        # .doc/.docx → PDF 转换后端：soffice 路径（未配置则尝试 PATH 中的 soffice，再退 Word COM）
        self.soffice_path: str = os.environ.get("KB_SOFFICE", "")

        # 文本打包
        self.text_merge_max_chars: int = int(os.environ.get("KB_TEXT_MERGE_MAX", "500"))

        # Agent 问答引擎（内核参数，业务无关）
        self.agent_max_rounds: int = int(os.environ.get("KB_AGENT_MAX_ROUNDS", "5"))
        self.agent_lookup_timeout: float = float(os.environ.get("KB_AGENT_LOOKUP_TIMEOUT", "90"))
        self.agent_total_timeout: float = float(os.environ.get("KB_AGENT_TOTAL_TIMEOUT", "120"))
        self.agent_history_turns: int = int(os.environ.get("KB_AGENT_HISTORY_TURNS", "6"))
        self.agent_history_budget: int = int(os.environ.get("KB_AGENT_HISTORY_BUDGET", "3000"))
        self.agent_tools_budget: int = int(os.environ.get("KB_AGENT_TOOLS_BUDGET", "8000"))

        # 模型（embedding/VL 走原生端点，chat 走兼容端点）
        self.embed_model: str = os.environ.get(
            "KB_EMBED_MODEL", "tongyi-embedding-vision-flash-2026-03-06")
        self.embed_dim: int = int(os.environ.get("KB_EMBED_DIM", "768"))
        self.chat_model: str = os.environ.get("KB_CHAT_MODEL", "qwen3.5-flash")
        self.vl_model: str = os.environ.get("KB_VL_MODEL", "qwen3.8-omni-flash")
        # 拒答阈值：通义 embedding 相似度整体偏高（无关 0.74~0.78 / 相关 0.83+），
        # 0.55 是 OpenAI 系分布的经验值，本模型按实测分布取 0.80
        self.reject_threshold: float = float(os.environ.get("KB_REJECT_THRESHOLD", "0.80"))

        for path in (self.data_dir, self.upload_dir, self.crop_dir):
            path.mkdir(parents=True, exist_ok=True)


settings = Settings()
