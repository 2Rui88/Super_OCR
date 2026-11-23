"""统一数据契约 —— 第一天定死，之后只增不改。

设计原则（借鉴 leaudit 生产架构）：
- 切块契约与来源无关：电子页(text_layer)和扫描页(ocr)产出同一种 blocks 结构，
  下游永远只面对一种格式，不关心这一页是"读出来的"还是"认出来的"。
- 位置一律归一化到 [0,1]（相对页面宽高），并附页面像素尺寸，
  前端任意分辨率渲染都能直接画框（加分项：原图框出字段位置）。
- 证件结果每个字段自带：值、置信度、bbox、校验结果，问题字段显式标出并写明原因。
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------- 基础类型

Bbox = list[float]  # [x1, y1, x2, y2]，归一化 0~1，相对页面左上角


class SourceMode(str, Enum):
    """每页识别方式 —— 必做项"结果中注明每一页用的是哪种方式"。"""

    TEXT_LAYER = "text_layer"  # 电子页：PDF 文字层直读（含 find_tables）
    IMAGE_OCR = "image_ocr"    # 电子页内嵌大图（流程图等）单独 OCR
    OCR = "ocr"                # 扫描页：整页 PP-StructureV3


class BlockType(str, Enum):
    """统一块标签 —— 两条路径（PP-StructureV3 / PyMuPDF）映射到同一套。"""

    TEXT = "text"
    TITLE = "title"
    TABLE = "table"
    FIGURE = "figure"
    FIGURE_CAPTION = "figure_caption"
    PAGE_HEADER = "page_header"
    PAGE_FOOTER = "page_footer"


# ---------------------------------------------------------------- 文档线

class Block(BaseModel):
    """页面切块：文字块 / 表格 / 图（流程图）等。"""

    type: BlockType
    text: str = ""                 # 纯文本（表格为行列拼接的影子文本首行摘要）
    bbox: Bbox = Field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0])
    confidence: float | None = None  # OCR 块的置信度；电子页直读为 None（视为 1.0）

    # 表格块专有：HTML 全程保留（禁止拍平），row_texts 为带表头的行影子文本
    html: str | None = None
    row_texts: list[str] = Field(default_factory=list)

    # 图块专有：图注绑定 + 流程图线性文本（节点文字按坐标排序）
    caption: str | None = None
    linear_text: str | None = None


class PageResult(BaseModel):
    page_num: int                  # 1-based
    source_mode: SourceMode        # 必做项：每页注明识别方式
    duration_ms: int = 0           # 加分项：每页耗时
    width_px: int = 0              # 页面像素尺寸（bbox 归一化的基准）
    height_px: int = 0
    blocks: list[Block] = Field(default_factory=list)

    @property
    def markdown(self) -> str:
        """按阅读顺序拼 Markdown（加分项）。"""
        parts: list[str] = []
        for b in self.blocks:
            if b.type == BlockType.TABLE and b.html:
                parts.append(b.html)
            elif b.type == BlockType.TITLE and b.text:
                parts.append(f"## {b.text}")
            elif b.text:
                parts.append(b.text)
        return "\n\n".join(parts)


class DocumentResult(BaseModel):
    """文档识别结果。"""

    file_sha256: str               # 内容哈希 —— 加分项：同文件不重复识别
    filename: str
    pages: list[PageResult] = Field(default_factory=list)
    total_duration_ms: int = 0
    quality_warnings: list[str] = Field(default_factory=list)  # 质量哨兵输出

    @property
    def markdown(self) -> str:
        return "\n\n".join(
            f"<!-- page {page.page_num}: {page.source_mode.value} -->\n{page.markdown}"
            for page in self.pages
        )


# ---------------------------------------------------------------- 证件线

class FieldValidation(BaseModel):
    passed: bool = True
    errors: list[str] = Field(default_factory=list)   # 人话原因，如"校验位错误，期望X实际Y"


class FieldResult(BaseModel):
    name: str
    value: str = ""
    confidence: float = 0.0        # 覆盖 OCR 行置信度的 min（真实识别概率，非模型自报）
    bbox: Bbox | None = None       # 字段在原图上的位置（加分项：画框高亮）
    validation: FieldValidation = Field(default_factory=FieldValidation)
    needs_review: bool = False     # 校验不过 或 置信度 < 阈值 → 标"有问题/建议复核"


class CardResult(BaseModel):
    """证件识别结果。"""

    file_sha256: str
    filename: str
    doc_type: str                  # id_front / id_back / license / driver / unknown
    doc_type_cn: str = "未知证件"
    type_confidence: float = 0.0   # 分类置信度
    fields: list[FieldResult] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)   # 汇总的问题说明（含"非4类证件"提示）
    duration_ms: int = 0
    det_side: int | None = None    # 实际生效的检测长边（低置信重试改走 1280 时记录）
    rectified_file: str | None = None  # 摆正图文件名（uploads 内 <sha>.rect.jpg）；字段
                                       # bbox 画在该图坐标系上，前端"摆正视图"切换用


class ImageRouteResult(BaseModel):
    """POST /cards 的统一信封：图片先按证件识别，未命中证件特征转文档识别。

    worker 实际以同构的裸 dict 产出（{"kind", "reason", "result"}），本模型
    描述该信封形状，供契约文档与类型提示引用。
    """

    kind: Literal["card", "document"]
    reason: str = ""               # kind=document 时说明转判原因
    result: CardResult | DocumentResult


# ---------------------------------------------------------------- 任务
# 任务轮询响应（GET /tasks/{id}）由 task_store 行直接组装为 dict（字段与
# 上述证件/文档契约一致），不再单独建模 TaskInfo；状态枚举值
# queued/running/done/failed 由任务库状态机保证。

