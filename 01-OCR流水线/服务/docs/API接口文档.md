# OCR 流水线服务 API 文档（v1）

Base URL：`http://<host>:8700/api/v1`（开发默认端口 8700，可用 `OCR_API_PORT` 覆盖）

## 总览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/cards` | 证件（图片）识别，支持同步等待或异步任务 |
| POST | `/documents` | 文档识别（PDF + Office 转换），始终异步 |
| GET | `/tasks/{task_id}` | 任务轮询：状态 / 进度 / 结果 / 失败原因 |
| POST | `/batches` | 批量上传（多图片），逐文件隔离失败 |
| GET | `/batches/{batch_id}` | 批次进度：逐文件状态 + 聚合计数 |
| POST | `/batches/{batch_id}/retry_failed` | 只重试失败项（attempts<3 且非永久失败） |
| GET | `/batches/{batch_id}/export?format=xlsx\|json` | 批次导出，证件号码按文本写入 |
| POST | `/eval` | 上传标准答案 + 指定批次 → 准确率/检出率报表 |
| GET | `/health` | 健康检查（SQLite 可写、worker 在线、模型加载状态） |

任务状态机：`queued → running → done | failed`（终态后不再变化）。

## POST /cards

`multipart/form-data`：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| file | 文件 | 是 | jpg / jpeg / png / bmp / webp，≤20MB（`OCR_MAX_UPLOAD_MB`） |
| type | 文本 | 否 | 证件类型提示：id_front / id_back / license / driver；不给则自动判断 |
| wait | 布尔 | 否 | 默认 true，同步等待最多 60 秒（`OCR_SYNC_WAIT_SECONDS`） |

**行为**：图片先经 PP-OCRv6 识别与证件分类。命中 4 类证件 → 复用同一次识别
做字段抽取与校验（`kind=card`）；未命中 → 自动按普通文档图片转 PP-StructureV3
识别（`kind=document`，reason 说明原因）。

**响应**：

- `200` 识别完成（同步等到了）：

```json
{
  "task_id": 1,
  "kind": "card",
  "reason": "",
  "result": {
    "file_sha256": "f26e…", "filename": "id_back_0_scan.jpg",
    "doc_type": "id_back", "doc_type_cn": "身份证反面", "type_confidence": 1.0,
    "fields": [
      {"name": "签发机关", "value": "示例市公安局示例分局", "confidence": 1.0,
       "bbox": [0.34, 0.65, 0.72, 0.72],
       "validation": {"passed": true, "errors": []}, "needs_review": false}
    ],
    "issues": [], "duration_ms": 14152
  }
}
```

- `202` 任务仍在进行（同步等待超时，或 `wait=false`）：

```json
{"task_id": 3, "status": "running", "message": "识别仍在进行，请轮询 GET /api/v1/tasks/{task_id}"}
```

> 202 不是错误。首次识别约 15~45 秒/张；文档图片兜底（转 PP-StructureV3）
> 首次可达 1~2 分钟（含模型加载）。相同内容文件会命中缓存，毫秒级返回。

- `422` 识别失败（文件损坏、worker 异常等），`error_code` 见错误码表。

## POST /documents

`multipart/form-data`：`file`（≤20MB）。接受 **PDF 与 Office 六格式**
（pdf / doc / docx / xls / xlsx / ppt / pptx；按文件头验真）。**始终**返回 `202`：

```json
{"task_id": 2, "status": "queued", "message": "文档识别为异步任务，请轮询 GET /api/v1/tasks/{task_id}"}
```

统一链路：Office 文件先由 LibreOffice 转 PDF（转换产物按内容哈希复用），
之后**所有文档逐页渲染走 PP-StructureV3**——不再区分文字层/扫描页，
`source_mode` 恒为 `ocr`，`engine_type` 恒为 `structure_v3`。相同内容
（原件哈希一致）的重复提交命中文档级结果缓存，秒级返回。

转换失败的错误码：`CONVERT_FAILED`（文件损坏/加密/转换超时）、
`DOC_TOO_LONG`（转换后超过 `OCR_MAX_DOCUMENT_PAGES`，默认 200 页），
均为永久失败，重试无意义。

## GET /tasks/{task_id}

```json
{
  "id": 2, "task_type": "document_scan", "status": "done",
  "phase": "完成", "progress": 1.0, "filename": "差旅费管理办法2021.pdf",
  "engine_type": "structure_v3", "attempts": 1, "worker_id": "worker-1",
  "error_code": null, "error_message": null,
  "created_at": "…", "started_at": "…", "finished_at": "…",
  "result": {"kind": "document", "reason": "", "result": {"pages": […], "…": "…"}}
}
```

- 文档任务运行中：`phase` 形如 `识别中 第2/8页`，`progress` 为 0~1。
- 证件任务的结果是统一信封（同 `/cards` 的 200 响应，去掉 task_id）。
- `404`：任务不存在（`TASK_NOT_FOUND`）。

**轮询建议**（项目二对接）：起步 2 秒一次，未到终态按 ×1.5 退避，封顶 10 秒；
`done`/`failed` 为终态即可停止。同一文件（内容哈希相同）重复提交证件识别会命中
缓存，可直接用 `/cards` 同步重取。

## POST /batches

`multipart/form-data`：`files`（可重复字段，多张图片）。单个文件不合法不影响
其他文件——非法文件落为**永久失败**的批次项并记录原因。响应 `202`：

```json
{"batch_id": 4, "total": 7, "accepted": 6, "rejected": 1,
 "message": "请轮询 GET /api/v1/batches/{batch_id} 查看逐文件进度"}
```

## GET /batches/{batch_id}

```json
{"batch_id": 4, "created_at": "…", "total": 7,
 "counts": {"queued": 0, "running": 0, "done": 6, "failed": 1},
 "progress": 1.0,
 "items": [{"task_id": 10, "filename": "bad.jpg", "status": "failed",
            "error_code": "UNSUPPORTED_FORMAT", "error_message": "文件内容不是有效的图片"},
           {"task_id": 8, "filename": "id_front_0_scan.jpg", "status": "done",
            "kind": "card", "doc_type": "id_front", "doc_type_cn": "身份证正面"}]}
```

`progress` = 终态项（done+failed）/ 总数。

## POST /batches/{batch_id}/retry_failed

只把 **failed 且可重试** 的任务重新排队：

```json
{"requeued": 1, "skipped_permanent": 1}
```

不可重试 = attempts 已达 3 次，或错误码为 `UNSUPPORTED_FORMAT` / `FILE_TOO_LARGE`
（上传即被拒）/ `CONVERT_FAILED` / `DOC_TOO_LONG`（转换确定性失败）。
相同内容文件重跑会命中缓存，秒级完成。

## GET /batches/{batch_id}/export?format=xlsx|json

- `json`：完整转储（每项含统一信封 result）；
- `xlsx`：人读报表，一行一文件，列为 文件名/证件类型/状态/各字段/问题与提示；
  **全表文本格式（`number_format="@"`）**，18 位身份证号不会变科学计数法。

## POST /eval

`multipart/form-data`：`batch_id`（表单）+ `ground_truth`（标准答案 JSON 文件，
结构同 `资料/公开样例/标准答案.json`；自造测试集另支持可选的 `invalid_fields`
数组标记故意做错的字段，用于统计错误检出率）。

响应（节选）：

```json
{"eval_id": 3, "batch_id": 1, "ground_truth_sha256": "…",
 "report": {
   "field_accuracy": 1.0, "correct_fields": 39, "total_fields": 39,
   "type_accuracy": 1.0,
   "by_type": {"身份证正面": {"correct": 12, "total": 12, "accuracy": 1.0}, "…": {}},
   "by_field": {"姓名": {"correct": 3, "total": 3, "accuracy": 1.0}, "…": {}},
   "wrong_fields_total": 0, "wrong_fields_flagged": 0,
   "planted_invalid_total": 0, "planted_invalid_flagged": 0,
   "planted_invalid_flag_rate": null,
   "pending_files": [], "items": []
 }}
```

口径：分类错或漏字段都计错；`wrong_field_flag_rate` = 抄错字段里被
`needs_review` 标出的比例（验收标准"识别错的字段有没有被标出来"）；
`planted_invalid_flag_rate` = 故意做错字段的检出率。批次未完成的文件列入
`pending_files`，不计入准确率。评测结果落 `eval_runs` 表留档。

## GET /health

```json
{"status": "ok", "components": {"sqlite_writable": true, "workers": [
  {"worker_id": "worker-1", "alive": true, "card_model_loaded": true,
   "document_model_loaded": false, "current_task_id": null}]}}
```

- `ok`：SQLite 可写且至少一个 worker 心跳新鲜；
- `degraded`：库正常但无 worker 在线（只影响新任务，历史任务可查）；
- `error`（HTTP 503）：SQLite 不可写。

## 错误码

| 场景 | HTTP | error_code | 说明 |
|---|---|---|---|
| 扩展名不在白名单 / 内容与类型不符 | 415 | UNSUPPORTED_FORMAT | 扩展名会撒谎，服务端按文件头验真 |
| 超过大小上限 | 413 | FILE_TOO_LARGE | 默认 20MB |
| 任务不存在 | 404 | TASK_NOT_FOUND | |
| 批次不存在 | 404 | BATCH_NOT_FOUND | |
| 标准答案不是合法 JSON 数组 | 422 | INVALID_GROUND_TRUTH | |
| 导出格式不支持 | 422 | UNSUPPORTED_FORMAT | format 只支持 json / xlsx |
| 识别失败（同步等到失败） | 422 | 见下 | |
| worker 执行异常 | — | WORKER_ERROR | 任务表 error_message 带异常详情 |
| worker 重启前中断 | — | SERVICE_RESTARTED | retry_failed 可重排 |
| worker 失联被回收 | — | WORKER_LOST | 心跳过期，任务自动标失败 |

**重试规则**：attempts < 3 且错误码非 `UNSUPPORTED_FORMAT`/`FILE_TOO_LARGE`
的 failed 任务可被 `retry_failed` 重新排队；attempts 到 3 次即永久失败，
避免毒丸文件无限消耗 OCR 资源。

## 部署形态

```
ocr-api（1 个）      只做 HTTP/上传/查询，永不加载模型；根路径托管前端页面
ocr-worker-1 / 2     平级；各持 1 个证件模型（4 线程），文档引擎懒加载；
                     一次一个任务；扫描文档全局并发默认 1、`OCR_MAX_RUNNING_DOCUMENTS`
                     可调（领取 SQL 保证，生产为 3）
SQLite（WAL）        tasks / workers 表 = 队列 + 状态机 + 心跳
```

前端页面（上传/进度/字段框高亮/批次/导出/评测）由 API 在根路径直接托管：`http://<host>:8700/`。

启动：生产环境统一用仓库根目录 `deploy/start.sh`（GPU、并发 3、预加载等
生产配置全部固化在脚本内）；环境安装与验证步骤见
[deploy/部署文档.md](../../../deploy/部署文档.md)。

环境变量：`OCR_TASK_DB`、`OCR_CACHE_DB`、`OCR_UPLOAD_DIR`、`OCR_SYNC_WAIT_SECONDS`、
`OCR_MAX_UPLOAD_MB`、`OCR_CARD_CPU_THREADS`、`OCR_DOCUMENT_CPU_THREADS`、
`OCR_API_HOST/PORT` 等，见 `app/config.py`。
