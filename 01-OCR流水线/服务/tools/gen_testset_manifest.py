"""为一批测试集目录写 生成清单.md（配合 gen_bulk_certs.py / gen_bulk_docs.py 使用）。

用法：
    python tools/gen_testset_manifest.py <测试集根目录> <批次说明> [生成日期]

示例：
    python tools/gen_testset_manifest.py C:/Users/Wren-GS/Desktop/OCR测试集3 "第三批（种子 20261121，与前两批零重复）" 2026-11-20
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path


def main() -> int:
    root = Path(sys.argv[1])
    note = sys.argv[2]
    day = sys.argv[3] if len(sys.argv) > 3 else "2026-11-20"
    truth = json.loads((root / "01_证件图片" / "标准答案.json").read_text(encoding="utf-8"))
    docs = sorted((root / "02_非图片文档").iterdir())
    cnt = Counter(t["doc_cn"] for t in truth)

    lines = [f"# OCR 测试集生成清单（{note.split('（')[0]}）", "",
             f"生成日期：{day}，{note}；复现命令见 `tools/gen_bulk_certs.py` / `tools/gen_bulk_docs.py`。",
             "", "## 一、证件图片（50 张）→ `01_证件图片/`", "",
             "覆盖服务全部 4 类证件，含多种拍摄/扫描扰动；字段标准答案见同目录 `标准答案.json`。", ""]
    lines += [f"- {k}：{v} 张" for k, v in cnt.items()]
    lines += ["",
              "扰动条件：scan 扫描 / rotate 旋转 / perspective 透视翻拍 / blur 模糊 / noise 噪点 / glare 反光 / uneven 光照不均 / watermark 水印 / wrap 地址折行 / long_term 长期证。",
              "", "**故意坏字段样本（应触发校验报错）：**", ""]
    for t in truth:
        if t.get("invalid_fields"):
            lines.append(f"- `{t['file']}`（{t['doc_cn']}）→ {', '.join(t['invalid_fields'])}")
    lines += ["", "## 二、非图片文档（50 份）→ `02_非图片文档/`", "",
              f"- PDF：{sum(1 for d in docs if d.suffix == '.pdf')} 份（合同/报告/纪要/制度/说明书/函件，含表格）",
              f"- DOCX：{sum(1 for d in docs if d.suffix == '.docx')} 份（通知/总结/方案/管理办法等）",
              f"- XLSX：{sum(1 for d in docs if d.suffix == '.xlsx')} 份（花名册/明细/台账/预算等表格）",
              f"- PPTX：{sum(1 for d in docs if d.suffix == '.pptx')} 份（汇报/发布/培训/宣导课件）", "",
              "全部为虚构内容（示例市/虚构公司与人名），可直接经上传接口走 Office→PDF 转换与 StructureV3 识别链路。",
              "", "## 文件明细", "", "### 证件图片", ""]
    lines += [f"- {t['file']}（{t['doc_cn']}，{t['cond']}）" for t in truth]
    lines += ["", "### 非图片文档", ""] + [f"- {d.name}" for d in docs]
    lines += ["", f"合计：{len(truth)} 张证件 + {len(docs)} 份文档 = {len(truth) + len(docs)} 个文件。"]

    (root / "生成清单.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"清单已写入 {root}（{len(truth)}+{len(docs)}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
