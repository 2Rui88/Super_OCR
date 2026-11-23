# OCR 流水线服务核心

当前提供：证件流水线、带缓存的常驻服务、统一文档链路（任意文件 → PDF → PP-StructureV3），以及 **HTTP 服务 + SQLite 任务库 + 双 OCR worker** 的服务化运行形态。接口契约见 [docs/API接口文档.md](docs/API接口文档.md)。

## 服务化运行（阶段1+2）

> **部署**：生产环境（GPU、文档并发 3、启动预加载、低置信重试 1280）的
> 环境安装、启动与验证全部见仓库根目录 [deploy/部署文档.md](../../deploy/部署文档.md)，
> 生产配置固化在 `deploy/start.sh`。代码内默认值（并发 1、CPU、不预加载）
> 仅为开发机保守配置。

- 图片走 `POST /api/v1/cards`：一次 PP-OCRv6 识别，命中证件直接抽字段，
  未命中自动转 PP-StructureV3 按文档图片识别（响应 `kind` 区分）；
- 文档走 `POST /api/v1/documents`：接受 PDF 与 Office 六格式（doc/docx/xls/
  xlsx/ppt/pptx）。Office 先经 LibreOffice 转 PDF（产物按内容哈希复用），
  之后**统一逐页走 PP-StructureV3**——不再区分文字层/扫描页，产出同构的
  标题/表格/阅读顺序结构；全走模型后识别成本高，同内容重复提交命中
  文档级结果缓存秒回；
- 任务库 `data/tasks.sqlite3`（WAL）承担队列/状态机/心跳，与结果缓存分离；
- 文档任务全局并发默认 1（`OCR_MAX_RUNNING_DOCUMENTS` 可调，领取 SQL 互斥；
  生产已调 3），证件任务多 worker 并行；
- worker 重启恢复自己遗留的 running 任务；失联由心跳收割（WORKER_LOST）。

**部署依赖**：Office 转换需要 LibreOffice + 中文字体（安装命令见
[deploy/部署文档.md](../../deploy/部署文档.md)，`OCR_SOFFICE_BIN` 可指定
soffice 路径）。缺中文字体时转换产物是豆腐块，OCR 结果不可用——字体是
转换质量的上限。转换相关环境变量：`OCR_SOFFICE_BIN` /
`OCR_CONVERT_TIMEOUT_SECONDS`（默认 120）/ `OCR_MAX_DOCUMENT_PAGES`
（默认 200，超限报 DOC_TOO_LONG）。

## 批量、导出与评测（阶段4/5）

```powershell
# 批量评测自造测试集（需 API + worker 在跑）：
.\.venv\Scripts\python.exe tools/batch_eval_cli.py --dir ..\资料\自造测试集 --out tests\_out\自造测试集评测.json
```

- `POST /api/v1/batches` 一次传多张：单文件失败不影响其他文件（非法文件落永久
  失败并记原因）；`retry_failed` 只重排 failed 且 attempts<3 的任务；
- `export?format=xlsx` 全表文本格式，18 位证号不会变科学计数法；
- `POST /api/v1/eval` 上传标准答案 + 批次 → 字段/类型准确率、按类型/按字段、
  抄错字段标出率、故意做错字段检出率，结果落 `eval_runs` 表。

## 自造测试集（阶段5）

```powershell
.\.venv\Scripts\python.exe tools\gen_test_certs.py --out ..\资料\自造测试集 --seed 20261118
```

4 类证件各 ≥10 张（共 43），全部虚构数据；覆盖 正常扫描/旋转/翻拍透视/模糊/
噪点/反光/光照不均/水印/地址换行/号码校验位错误/日期非法/有效期倒挂/长期。
固定随机种子可复现；号码用 `app.certs.validators` 生成时自检（合法必过、
故意错必被抓）；每张图的扰动参数存 `生成参数.json`，标准答案含 `invalid_fields`
标记供错误检出率统计。

**最终评测（43 张全量，`tests/_out/自造测试集评测.json`）**：

- 字段准确率 **264/273 = 96.70%**，类型准确率 100%（43/43）；
- 故意做错字段（错号/错日期/倒挂）检出率 **7/7 = 100%**；
- 13 个扰动条件中 11 个 100%；剩余难点：水印 84%（戳压文字）、透视 88%；
- 按类型：身份证正面 62/66、身份证反面 20/20、营业执照 84/88、驾驶证 98/99。

测试集经四轮迭代修正（67.03% → 85.35% → 87.91% → 96.70%），每轮均有
根因定位：小字号形近字、id_back 值行无横向重叠、满宽地址被旋转裁尾、
标签贴左边距被预处理裁掉偏旁（注→主/姓→生）。详见 git 历史。

## 3 份 PDF 文档验收结论（阶段6）

| 文档 | 方式 | 结论 |
|---|---|---|
| 差旅费管理办法（文字层 4 页） | PyMuPDF 直读 | 字级 98.82%、表格单元格 16/16、结构 100%、方式判定 100%（golden 为文字层自提取，可信） |
| 质量手册 QM-2025（扫描 8 页） | PP-StructureV3 | 服务化全量跑通（方式判定 100%）；绝对准确率**未宣称**（见下） |
| 印章管理规定（盖章扫描 4 页） | PP-StructureV3 | 同上；首页与参考转写交叉一致 98.87%，证明链路正确 |

**诚实声明（重要）**：扫描件 golden 初稿由 AI 视觉逐页转写生成，实测发现其存在
跳读与幻觉（转写内容结构合理但与实际页面不符），只能作参考，不能当真值。
因此扫描页只报告可证实的结论：a) 首页交叉一致 98.87% 证明识别与评测链路正确；
b) 扫描页识别确有丢字（如"第 X 条"丢序数）与阅读顺序问题，列为已知限制；
c) 印章/方向矫正开关用**同一参考尺的相对比较**决策（A/B 报告见
`tests/_out/ab_印章开关对比.json`），不依据绝对值。最终人工校对 golden 留待
验收前补做（工具与口径均已就绪）。

**印章/方向矫正 A/B 结论（4 变体 × 印章 4 页，同一参考尺）**：
default 0.3601 / seal_on 0.3601 / orientation_on 0.3609 / 全开 0.3609——
准确率零提升（≤0.08% 噪声级），按"不赢准确率不进默认配置"规则，**两者保持默认关闭**；
各变体墙钟差异属机器负载噪声，不作为依据。

## 已知边界（复查记录，均不阻塞交付）

- 图片经"证件→文档"兜底会加载 PP-StructureV3，此路径不受文档并发互斥约束
  （极端情况下两张非证件图同时进两个 worker 可同时加载文档引擎）；
- worker 所在机器休眠超过心跳窗口（30s）会被收割重跑，唤醒后可能重复写回
  同一任务（后写覆盖，结果幂等，无脏数据风险）；
- `GET /batches/{id}` 进度轮询会解析全部结果 JSON，证件批次毫秒级无感，
  数百文件的文档大批次偏重（可后续加摘要列优化）；
- 扫描文档全局并发上限已配置化：`OCR_MAX_RUNNING_DOCUMENTS`（默认 1），
  大内存机器可调高。

## 证件识别直调（工具/实验用）

日常使用走 HTTP 服务（见文首"服务化运行"）；需要绕过服务直调引擎时，
用与服务同构的原语——引擎识读一次，命中证件直接抽字段（`build_card_result`
复用同一批 OCR 行，不做二次识别）：

```python
from app.certs import load_certs
from app.core.card_pipeline import build_card_result
from app.engine.paddle_card import PaddleCardEngine

registry = load_certs("config/certs")
engine = PaddleCardEngine()  # 加载一次，在多个文件间复用
try:
    lines = engine.recognize_lines("example.jpg")
    result = build_card_result("example.jpg", lines, registry)
    print(result.model_dump_json())
finally:
    engine.close()
```

不要让多个线程共享同一个 PaddleOCR predictor，也不要在每张图片上重新创建进程或模型。
历史上曾有 `CardService` / `CardProcessPoolService`（常驻服务 + 进程池 + 自带缓存）
的进程内形态，已随 worker 化架构下线：并发与缓存由平级 worker 进程和
`data/tasks.sqlite3` 队列承担（见 `app/worker.py`）。

批量识别与失败隔离走 `POST /api/v1/batches`（单文件失败不影响其他文件，
可 `retry_failed` 重排）；结果缓存（内容哈希 + 引擎签名）同样在 worker 层，
相同内容重复提交秒回。

默认保留 medium 检测和识别、整页方向、行方向及 UVDoc。检测长边限制为 960，CPU 线程数为 4。需要更高检测分辨率时，可使用：

```python
engine = PaddleCardEngine(config=PaddleCardConfig(detection_max_side=1280))
```

可通过 `detection_model_name`、`recognition_model_name` 显式指定模型。tiny 和 v5 实验没有通过本轮准确率检查，因此不作为默认值。没有依赖校验位失败无限重试；样例中的虚构号码本来就可能不满足校验位。

## 统一文档链路（文字层快路径已下线）

```python
from app.core import process_document

result = process_document('example.pdf')
print(result.model_dump_json())
print(result.markdown)
```

所有 PDF（含 Office 转换件）逐页渲染 → PP-StructureV3，不再按文字层/扫描页
分流：text_layer 直读与 OCR 两种路径的块粒度/结构不一致，下游切块质量参差不齐，
统一后产出同构的标题/表格/阅读顺序结构。代价是每页都过模型（GPU 上约秒级/页），
重复提交由 worker 的文档级结果缓存吸收。引擎针对制度文档关闭公式、图表
专用模型，保留表格识别；印章识别已开启（GPU 实测零开销，且关闭时章面文字
会混入正文块，开启后独立成 seal 块，见 `tools/ab_seal_test.py`）；
也可注入 `scan_reader(image_bgr, page_num)`。

历史背景：曾经的文字层快路径（`source_mode=text_layer`）在项目二侧还需要
"text_layer 且图像占比高 → 补 OCR"的补偿逻辑，统一后该场景不复存在。

## 历史性能实验数据

性能优化战役（进程池对照、mkldnn/检测长边 profile、缓存命中验证）的实验
脚本已随旧架构下线清理，**测量数据全部保留**在 `tests/_out/performance/`
（含 [性能对比](tests/_out/performance/性能对比.md)），结论已沉淀进默认配置
（Paddle 3.2.2 + mkldnn 白名单、检测长边 960、CPU 线程 4）。原始
2026-11-17 冒烟结果仍保留在 `tests/_out/smoke_card_results.json`；普通冒烟
脚本写入 `smoke_card_latest.json`。字段准确率严格按标准答案的完整字段集合
计算，分类错误和漏字段都会计错。
