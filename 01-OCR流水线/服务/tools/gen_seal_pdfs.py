"""生成带红色印章的测试 PDF（虚构公司，PIL 画章 + PyMuPDF 排版）。

产物默认写 tests/_out/seal_test/，供 tools/ab_seal_test.py 做印章识别 A/B。
印章为程序绘制（环形文字 / 五角星 / 半透明 / 微旋转），非扫描件，但对
PP-StructureV3 的 seal 子流水线（红章检测 + 章内文字识别）是有效输入。
公司与人物均为虚构，与资料/自造测试集同一虚构世界观（澄川市）。

用法（服务目录）：
    .venv/Scripts/python.exe tools/gen_seal_pdfs.py [--out tests/_out/seal_test]
"""
from __future__ import annotations

import argparse
import io
import math
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image, ImageDraw, ImageFont

RED = (198, 20, 24)          # 印泥红
INK_ALPHA = 205              # 印章不透明度（模拟盖印深浅）
FONT_CANDIDATES = [          # Windows 自带中文字体，按序取第一个存在的
    r"C:/Windows/Fonts/msyh.ttc",
    r"C:/Windows/Fonts/simhei.ttf",
    r"C:/Windows/Fonts/simsun.ttc",
]
A4_W, A4_H, MARGIN = 595, 842, 72


def load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    raise SystemExit("未找到中文字体，请检查 C:/Windows/Fonts")


def _char_img(char: str, font: ImageFont.FreeTypeFont) -> Image.Image:
    canvas = Image.new("RGBA", (font.size * 2, font.size * 2), (0, 0, 0, 0))
    ImageDraw.Draw(canvas).text((font.size // 2, font.size // 2), char,
                                font=font, fill=(*RED, INK_ALPHA))
    bbox = canvas.getbbox()
    return canvas.crop(bbox) if bbox else canvas


def _arc_text(canvas: Image.Image, text: str, center: tuple[float, float],
              radius: float, theta_from: float, theta_to: float,
              font: ImageFont.FreeTypeFont, *, bottom: bool = False) -> None:
    """沿圆弧逐字摆放。θ 采用时钟制（0=正上，顺时针增，度）。

    top：字面朝外，阅读顺序左→右（θ 从 -A 到 +A）；
    bottom：字面朝圆心（章底文字的正确朝向），阅读顺序左→右
    （θ 从 180+A 递减到 180-A）。
    """
    cx, cy = center
    n = len(text)
    step = (theta_to - theta_from) / (n - 1) if n > 1 else 0.0
    for i, char in enumerate(text):
        theta = theta_from + step * i
        rad = math.radians(theta)
        x = cx + radius * math.sin(rad)
        y = cy - radius * math.cos(rad)
        # PIL rotate 逆时针为正：顶弧字顺时针转 θ；底弧字逆时针转 (180-θ)
        pil_angle = (180 - theta) if bottom else (-theta)
        stamp = _char_img(char, font).rotate(pil_angle, expand=True,
                                             resample=Image.BICUBIC)
        canvas.alpha_composite(stamp, (round(x - stamp.width / 2),
                                       round(y - stamp.height / 2)))


def _star(draw: ImageDraw.ImageDraw, center: tuple[float, float],
          r_out: float) -> None:
    r_in = r_out * 0.382
    points = []
    for i in range(10):
        r = r_out if i % 2 == 0 else r_in
        a = math.radians(-90 + i * 36)
        points.append((center[0] + r * math.cos(a),
                       center[1] + r * math.sin(a)))
    draw.polygon(points, fill=(*RED, INK_ALPHA))


def draw_seal(company: str, bottom_text: str = "", size: int = 640) -> Image.Image:
    """圆形印章：环形单位名 + 中央五角星 +（可选）底部章类型文字。"""
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    half = size / 2
    ring = 16
    draw.ellipse([ring, ring, size - ring, size - ring],
                 outline=(*RED, INK_ALPHA), width=ring)
    _arc_text(canvas, company, (half, half), half - 62, -118, 118,
              load_font(56))
    if bottom_text:
        _arc_text(canvas, bottom_text, (half, half), half - 70, 230, 130,
                  load_font(42), bottom=True)
    _star(draw, (half, half), size * 0.155)
    return canvas


def stamp(page: fitz.Page, seal: Image.Image, cx: float, cy: float,
          d_pt: float, jitter_deg: float) -> None:
    """把印章 PNG（微旋转模拟手工盖章）盖到页面 (cx, cy)，直径 d_pt 磅。"""
    img = seal.rotate(jitter_deg, expand=True, resample=Image.BICUBIC)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    rect = fitz.Rect(cx - d_pt / 2, cy - d_pt / 2, cx + d_pt / 2, cy + d_pt / 2)
    page.insert_image(rect, stream=buf.getvalue(), alpha=True, overlay=True)


def _text(page, x, y, s, size=12):
    page.insert_text((x, y), s, fontname="china-s", fontsize=size,
                     color=(0, 0, 0))


def _center(page, y, s, size=16):
    w = fitz.get_text_length(s, fontname="china-s", fontsize=size)
    _text(page, (A4_W - w) / 2, y, s, size)


def _right(page, y, s, size=12):
    w = fitz.get_text_length(s, fontname="china-s", fontsize=size)
    _text(page, A4_W - MARGIN - w, y, s, size)


def gen_notice(out: Path, seal: Image.Image) -> None:
    doc = fitz.open()
    page = doc.new_page(width=A4_W, height=A4_H)
    _center(page, 96, "关于调整差旅费报销时限的通知")
    lines = [
        "各部门、各子公司：",
        "为规范差旅费用管理，提高报销效率，现将差旅费报销时限调整事项通知如下：",
        "一、员工出差结束后，应当在三十日内完成报销申请，逾期未申请的，",
        "财务部门原则上不再受理。",
        "二、报销材料包括出差审批单、交通票据、住宿发票及差旅费报销单。",
        "三、特殊情况需延期的，应当提前向财务部门提交书面说明。",
        "本通知自2026年12月1日起施行。",
        "特此通知。",
    ]
    for i, line in enumerate(lines):
        _text(page, MARGIN, 140 + i * 24, line)
    _right(page, 380, "澄川市恒信科技有限公司")
    _right(page, 406, "2026年11月18日")
    stamp(page, seal, 450, 372, 150, 4)      # 公章盖落款，压单位名与日期
    doc.save(out)
    doc.close()


def gen_contract(out: Path, seal: Image.Image) -> None:
    doc = fitz.open()
    page = doc.new_page(width=A4_W, height=A4_H)
    _center(page, 96, "技术服务合同（节选）")
    lines = [
        "甲方：澄川市数智科技有限公司",
        "乙方：澄川市恒信科技有限公司",
        "第一条 服务内容：乙方为甲方提供办公系统运维服务，服务期限自",
        "2026年1月1日至2026年12月31日。",
        "第二条 服务费用：全年服务费人民币壹拾贰万元整（¥120,000），",
        "按季度支付，甲方于每季度首月十日前支付当季费用。",
        "第三条 保密义务：双方对履行本合同过程中知悉的商业秘密负有保密",
        "责任，保密期限为合同终止后两年。",
        "（以下无正文）",
    ]
    for i, line in enumerate(lines):
        _text(page, MARGIN, 140 + i * 24, line)
    _text(page, MARGIN, 396, "甲方（盖章）：")
    _text(page, 330, 396, "乙方（盖章）：澄川市恒信科技有限公司")
    _text(page, MARGIN, 424, "日期：2026年11月2日")
    _text(page, 330, 424, "日期：2026年11月2日")
    stamp(page, seal, 440, 400, 145, -3)     # 合同专用章盖乙方签署处
    doc.save(out)
    doc.close()


def gen_certificate(out: Path, seal_a: Image.Image, seal_c: Image.Image) -> None:
    doc = fitz.open()
    page = doc.new_page(width=A4_W, height=A4_H)
    _center(page, 110, "在职证明")
    lines = [
        "兹证明王建军（身份证号：3205**********0017）系我司正式员工，",
        "自2020年6月起在我司工作，现任行政部主管职务，月收入为人民币",
        "壹万贰仟元整。",
        "本证明仅用于办理银行贷款手续，不作其他用途。特此证明。",
    ]
    for i, line in enumerate(lines):
        _text(page, MARGIN, 160 + i * 24, line)
    _right(page, 340, "澄川市恒信科技有限公司")
    _right(page, 366, "2026年11月20日")
    stamp(page, seal_a, 297, 130, 150, 2)    # 公章压标题（常见盖法）
    stamp(page, seal_c, 440, 348, 135, -5)   # 财务专用章压落款
    doc.save(out)
    doc.close()


def _table(page: fitz.Page, x: float, y: float, col_ws: list[float],
           rows: list[list[str]], row_h: float = 24) -> None:
    """极简表格：外框 + 行线 + 列线 + 单元格文字。"""
    total_w = sum(col_ws)
    total_h = row_h * len(rows)
    for r in range(len(rows) + 1):
        page.draw_line((x, y + r * row_h), (x + total_w, y + r * row_h))
    cx = x
    for w in col_ws[:-1]:
        cx += w
        page.draw_line((cx, y), (cx, y + total_h))
    for r, row in enumerate(rows):
        cx = x
        for w, cell in zip(col_ws, row):
            _text(page, cx + 6, y + (r + 1) * row_h - 7, cell, 11)
            cx += w


def gen_approval_form(out: Path, seal_a: Image.Image,
                      seal_c: Image.Image) -> None:
    """两页：第 1 页公文带公章；第 2 页用印审批表，财务章压在表格上。"""
    doc = fitz.open()
    page = doc.new_page(width=A4_W, height=A4_H)
    _center(page, 96, "关于开展2026年度合同专项检查的通知")
    lines = [
        "各部门、各子公司：",
        "为加强合同履约管理，防范经营风险，公司决定开展2026年度合同专项检查，",
        "现将有关事项通知如下：",
        "一、检查范围：2025年1月1日至2026年10月31日期间签订的全部合同。",
        "二、检查内容包括合同审批手续完整性、履约情况、款项支付进度。",
        "三、各部门应当于2026年12月10日前完成自查并填报《用印审批单》。",
        "特此通知。",
    ]
    for i, line in enumerate(lines):
        _text(page, MARGIN, 140 + i * 24, line)
    _right(page, 360, "澄川市恒信科技有限公司")
    _right(page, 386, "2026年11月21日")
    stamp(page, seal_a, 450, 352, 150, 3)

    page2 = doc.new_page(width=A4_W, height=A4_H)
    _center(page2, 96, "用印审批单")
    _text(page2, MARGIN, 130, "申请部门：行政部    申请人：王建军    日期：2026年11月21日")
    _table(page2, MARGIN, 156, [150, 130, 173], [
        ["用印事项", "审批人", "备注"],
        ["一般文件、证明", "部门负责人", "当日办结"],
        ["合同、协议用印", "总经理", "附合同文本"],
        ["对外担保、大额资金", "董事长", "专项报告"],
        ["财务票据专用章", "财务负责人", "限量领用"],
    ])
    _text(page2, MARGIN, 330, "备注：本审批单作为印章使用登记凭证，保存期限五年。")
    stamp(page2, seal_c, 430, 260, 135, -4)  # 财务章压在表格下部
    doc.save(out)
    doc.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path,
                        default=Path(__file__).resolve().parents[1]
                        / "tests" / "_out" / "seal_test")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    company_seal = draw_seal("澄川市恒信科技有限公司")
    contract_seal = draw_seal("澄川市恒信科技有限公司", "合同专用章")
    finance_seal = draw_seal("澄川市恒信科技有限公司", "财务专用章")

    gen_notice(args.out / "01_notice_gongzhang.pdf", company_seal)
    gen_contract(args.out / "02_contract_seal.pdf", contract_seal)
    gen_certificate(args.out / "03_certificate_dual_seal.pdf",
                    company_seal, finance_seal)
    gen_approval_form(args.out / "04_approval_form_2pages.pdf",
                      company_seal, finance_seal)
    for pdf in sorted(args.out.glob("*.pdf")):
        print(f"生成 {pdf}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
