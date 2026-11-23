# 制度知识库服务（p2-kb）

制度文件智能问答：任意格式制度文件统一转 PDF 后经**项目一 OCR 接口**识别，
块级入库（章节面包屑 + 坐标档案），双路混合检索（FAISS 向量 + FTS5 关键词），
流式生成带精确出处的回答，答不出明确拒答。设计与方案见 `../方案.md`、`../实施方案.md`。

## 架构

```
前端 5 屏（app/static/index.html，服务端直接托管）
   │ HTTP / SSE
p2-kb（FastAPI + SQLite WAL + FAISS + DashScope）
   │ POST /api/v1/documents → 202 轮询（契约冻结，见 01-OCR流水线/服务/docs/API接口文档.md）
p1-ocr（项目一，API:8700 + 双 worker，PaddleOCR）
```

要点：SQLite 是数据真身（documents/chunks/会话/评测 7 张表 + FTS5 trigram 触发器同步），
FAISS 索引只是可再生缓存（向量以 BLOB 存库，重建零 API 费用）；摄入队列使用**心跳租约**
（documents 行即租约：领单抢占 / 30s 续租 / 5 分钟无心跳回收 / 丢租约自动止损），无启动清扫。

## 运行（开发，Windows）

前置：项目一服务在跑（默认 `http://127.0.0.1:8700`）；LibreOffice 已安装（.doc/.docx 转换）。

```powershell
cd 02-制度知识库/服务
python -m venv .venv
.\.venv\Scripts\pip.exe install -r requirements.txt
copy .env.example .env                                        # 填 DASHSCOPE_API_KEY
.\.venv\Scripts\python.exe -m uvicorn app.api.app:app --port 8800
```

打开 `http://127.0.0.1:8800/`（问答 / 文档库 / 评测三屏）。摄入队列内嵌于 API 进程，
无需单独 worker 进程。


## API 概览（`/api/v1`）

| 接口 | 说明 |
|---|---|
| `POST /documents` | 批量导入（multipart files + replaces=旧文档id 实现版本替代），秒回入队 |
| `GET /documents` | 文档库列表 + 统计卡数据 |
| `GET /documents/{id}` | 解析详情（目录结构 + 块列表） |
| `POST /documents/{id}/reparse` | 重新解析（就绪文档从存档 JSON 重放，不重烧 OCR；处理中文档由租约闭环自动止损） |
| `DELETE /documents/{id}` | 删除（清块 + FAISS 增量摘 + 旧版恢复可见） |
| `POST /index/rebuild` | 重建索引（SQLite BLOB 重放，零 API 费） |
| `GET /queue/stats` | 队列水位（pending/processing/failed + 最老任务年龄） |
| `POST /qa/ask` | SSE 问答（citations → delta* → refuse/done），支持 conversation_id 多轮 |
| `GET /chunks/{id}` | 出处详情（正文 bbox / 表格 HTML+行号 / 流程图裁图） |
| `GET /crops/{id}` | 流程图裁图 PNG |
| `GET /documents/{id}/file` | 下载原件 |
| `GET/POST /eval/*` | 评测：CSV 导入 / 一键运行 / 进度与逐题明细（含归因） |

## 关键实现位置

| 模块 | 职责 |
|---|---|
| `app/queue.py` | 心跳租约（领单/续租三态/回收/持有者终态/水位） |
| `app/worker.py` | 消费循环（执行+心跳双协程，丢租约 cancel 止损） |
| `app/pipeline.py` | 摄入 A2–A7：转 PDF → P1 识别存档 → 章节面包屑 → 分块 → 向量化上架 |
| `app/figures.py` | 流程图裁图（144dpi 与 P1 同基准）+ VL 描述 + 节点名交叉校验 |
| `app/retrieval.py` | 双路检索 + RRF + 双路皆弱拒答（trigram 窗口 OR + 双字词 LIKE 回退） |
| `app/llm.py` | DashScope（embedding/VL 原生端点、chat 兼容端点），Semaphore(8)+429 退避 |
| `app/vector.py` | FAISS IndexIDMap2（Python 字节流落盘——Windows 中文路径坑） |
| `app/evalservice.py` | 评测执行：LLM 判分 + 出处比对 + OCR/检索/回答三分类归因 |

## 自测

```powershell
# 摄入冒烟（走真实全链路，观察状态流转）
.\.venv\Scripts\python.exe tests\smoke_ingest.py ..\资料\制度文件\差旅费管理办法2021.pdf
# 守望批量导入进度
.\.venv\Scripts\python.exe tests\watch_ingest.py
```

评测：`tests/评测集.csv` → 前端"评测集"页导入 → 全部运行 → 五项指标与逐题归因。

## 已知问题与边界

1. P1 无任务取消/内容去重接口，P2 已加租约感知自保（重试前检查持有），但 P1 侧仍建议补接口；
2. 扫描件 OCR 置信度低的块（如条号数字丢失）会降低该类题正确率——评测归因会标为 `ocr`；
3. 拒答阈值 0.80 为本机实测分布校准值（通义 embedding 无关 0.74~0.78 / 相关 0.83+），换模型需重校；
4. 数字型细节查询（节点编号、公司地址类）排序偏弱，评测已知 4 题为此类；
5. 多轮改写依赖 LLM，无密钥时退化为按原始问题检索。

## 评测结果（终版 run 7）

总正确率 89% · 出处 97% · 表格 75% · 查无拒答 100% · 流程图 4/4 · 问答实测 3~4 秒。
四轮校准过程与逐题归因见 `../自测报告.md` 第五节。
