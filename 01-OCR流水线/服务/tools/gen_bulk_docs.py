"""批量生成 50 份非图片型测试文档（PDF/DOCX/XLSX/PPTX，全部虚构内容，固定种子）。

用法（任意目录，全局 Python，需 reportlab/python-docx/openpyxl/python-pptx）：
    python tools/gen_bulk_docs.py [输出目录]

配额：PDF 20 + DOCX 15 + XLSX 10 + PPTX 5 = 50。
内容均为虚构（示例市/示例省、虚构公司与人名），与 gen_test_certs 数据池同风格。
PDF/DOCX 共用同一份"文档模型"（标题/元信息/条目/落款），保证两类格式内容同构可对照。
"""
from __future__ import annotations

import random
import sys
from datetime import date, timedelta
from pathlib import Path

def _parse_args():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out", nargs="?", type=Path,
                        default=Path("C:/Users/Wren-GS/Desktop/OCR测试集/02_非图片文档"))
    parser.add_argument("--seed", type=int, default=20261119)
    return parser.parse_args()


_ARGS = _parse_args()
RNG = random.Random(_ARGS.seed)
OUT = _ARGS.out

# ---------------------------------------------------------------- 虚构数据池
SURNAMES = "赵钱孙李周吴郑王冯陈蒋沈韩杨朱秦许吕施张孔曹严金魏陶姜"
GIVEN = ["伟", "芳", "娜", "秀英", "敏", "静", "丽", "磊", "军", "洋", "勇",
         "艳", "杰", "涛", "明超", "霞", "建国", "雨萱", "子墨", "清和"]
COMPANIES = [
    "示例市星河智能科技有限公司", "示例市江澜文化传媒股份有限公司",
    "示例云脊信息技术有限公司", "示例市沧月供应链管理有限公司",
    "示例市天工建筑装饰工程有限公司", "示例市铭川信息技术有限公司",
    "示例市青梧文化传媒有限公司", "示例市岚图智能装备有限公司",
]
ADDRESSES = [
    "示例省示例市滨河区望江路128号锦绣花园7栋", "示例市朝阳区科创路8号数字大厦15层",
    "示例自治区示例市青云县翠屏镇东风街道幸福里巷46号", "示例省临江县白沙镇文化街33号",
]
DEPTS = ["综合管理部", "财务部", "人力资源部", "市场部", "研发中心", "采购部", "质量管理部", "安全环保部"]
PROJECTS = ["智慧园区一卡通平台", "仓储管理系统升级", "客户服务热线整合",
            "生产设备物联改造", "数据中心机房扩容", "移动办公门户建设"]


def person() -> str:
    return RNG.choice(SURNAMES) + RNG.choice(GIVEN)


def company() -> str:
    return RNG.choice(COMPANIES)


def cn_date(d: date) -> str:
    return f"{d.year}年{d.month}月{d.day}日"


def rand_date(y0: int = 2024, y1: int = 2026) -> date:
    return date(RNG.randrange(y0, y1 + 1), RNG.randrange(1, 13), RNG.randrange(1, 29))


def money(low: int = 5, high: int = 800) -> str:
    return f"{RNG.randrange(low, high)}.{RNG.choice('05')}万元" if RNG.random() < 0.6 \
        else f"{RNG.randrange(low, high)}万元"


def no(prefix: str) -> str:
    return f"{prefix}〔{RNG.randrange(2024, 2027)}〕{RNG.randrange(1, 120):03d}号"


# ---------------------------------------------------------------- 文档模型
class Doc:
    """PDF 与 DOCX 共用：title / meta / items(条目) / sign(落款)。"""

    def __init__(self, title: str, meta: list[str] | None = None):
        self.title, self.meta, self.items, self.sign = title, meta or [], [], []

    def h(self, text: str):
        self.items.append(("h", text))

    def p(self, text: str):
        self.items.append(("p", text))

    def table(self, header: list[str], rows: list[list[str]]):
        self.items.append(("table", header, rows))


# ---------------------------------------------------------------- 内容素材
CLAUSES = {
    "采购合同": [
        ("标的与数量", "乙方向甲方供应本合同附件所列货物，货物的名称、规格、数量及单价以附件《货物清单》为准。"),
        ("质量标准", "货物质量应符合国家标准及行业通行标准，并随货提供出厂合格证与检测报告。"),
        ("价款与支付", "合同总价为人民币{amount}（含税）。货物验收合格后{days}个工作日内，甲方支付至合同总价的95%，余款作为质保金，质保期满无质量问题后一次付清。"),
        ("交付与验收", "乙方应于{date}前将货物运抵甲方指定地点。甲方应在到货后7个工作日内完成验收，验收不合格的，乙方应在10个工作日内更换或修复。"),
        ("违约责任", "乙方逾期交付的，每逾期一日按合同总价的0.5‰支付违约金；甲方逾期付款的，每逾期一日按应付未付金额的0.5‰支付违约金。"),
        ("争议解决", "因本合同引起的争议，双方应友好协商；协商不成的，提交示例市仲裁委员会仲裁。"),
    ],
    "房屋租赁合同": [
        ("租赁标的", "甲方将位于{addr}的房屋出租给乙方使用，建筑面积约{area}平方米，用途为办公。"),
        ("租赁期限", "租赁期自{date}起至{date2}止，租期{years}年。"),
        ("租金与支付", "月租金人民币{rent}元，按季度支付，乙方应于每季度首月5日前支付当季租金。"),
        ("押金", "乙方应于签约时向甲方支付相当于两个月租金的押金，租赁期满且无欠费、无损坏的，甲方无息退还。"),
        ("维修责任", "房屋主体结构由甲方负责维修；因乙方使用不当造成的损坏，由乙方负责修复或赔偿。"),
        ("违约责任", "任何一方提前解除合同的，应提前30日书面通知对方，并按一个月租金的标准支付违约金。"),
    ],
    "劳动合同": [
        ("合同期限", "本合同为固定期限劳动合同，自{date}起至{date2}止，其中试用期{months}个月。"),
        ("工作内容", "甲方聘用乙方在{dept}担任{post}岗位工作，工作地点为示例市。"),
        ("劳动报酬", "乙方月工资为人民币{salary}元，甲方于每月15日前发放上一自然月工资。"),
        ("工作时间与休假", "甲方实行标准工时制度，乙方依法享受法定节假日及带薪年休假。"),
        ("社会保险", "甲方依法为乙方缴纳社会保险费，个人承担部分由甲方从乙方工资中代扣代缴。"),
        ("合同解除", "双方协商一致可以解除本合同；符合《劳动合同法》规定情形的，按法律规定执行。"),
    ],
    "保密协议": [
        ("保密信息", "本协议所称保密信息，包括甲方的技术资料、经营数据、客户名单、财务信息及其他未公开信息。"),
        ("保密义务", "乙方不得以任何方式向第三方披露、泄露保密信息，未经甲方书面同意不得使用于本职工作之外的目的。"),
        ("保密期限", "保密义务自本协议签署之日起生效，至该信息公开或甲方书面豁免之日止，不因劳动关系解除而失效。"),
        ("违约责任", "乙方违反本协议的，应向甲方支付违约金人民币{amount}；造成损失超过违约金的，还应赔偿差额部分。"),
    ],
    "借款协议": [
        ("借款金额", "甲方向乙方出借人民币{amount}，用于乙方{use}。"),
        ("借款期限", "借款期限自{date}起至{date2}止，共{months}个月。"),
        ("利息", "借款利率为年利率{rate}%，自实际放款之日起计息。"),
        ("还款方式", "借款到期后一次性还本付息；乙方提前还款的，按实际借款天数计息。"),
        ("违约责任", "乙方逾期还款的，按每日0.5‰支付逾期利息，并承担甲方实现债权的合理费用。"),
    ],
    "服务外包合同": [
        ("服务内容", "乙方为甲方提供{svc}服务，服务的范围、标准与考核细则以附件《服务说明书》为准。"),
        ("服务期限", "服务期自{date}起至{date2}止。"),
        ("服务费用", "服务费总计人民币{amount}，按月结算，乙方开具增值税专用发票后10个工作日内支付。"),
        ("服务质量", "乙方应保证月度服务达标率不低于95%；未达标的，按未达标比例扣减当月服务费。"),
        ("保密条款", "乙方在服务过程中接触到的甲方数据与资料，均按双方另行签署的保密协议执行。"),
    ],
}

REPORT_FINDINGS = [
    "报告期内营业收入{rev}万元，同比增长{pct}%，收入结构以主营业务为主，总体经营态势平稳。",
    "期间费用合计{cost}万元，费用率与上年基本持平，其中销售费用受市场活动节奏影响略有波动。",
    "应收账款余额{rev2}万元，账龄一年以内的占比约八成，回款情况总体可控。",
    "库存周转天数约{days}天，较上年缩短，供应链协同效率有所提升。",
    "报告期内未发生重大安全环保事故与重大合规风险事件，内控制度运行总体有效。",
    "主要客户与供应商结构保持稳定，前五大客户收入占比{pct2}%，客户集中度处于合理区间。",
]
REPORT_ADVICE = [
    "建议持续优化产品结构，加大高毛利产品的资源投入，提升整体盈利能力。",
    "建议加强应收账款过程管理，对长账龄客户实行专人跟进与限额管控。",
    "建议完善预算刚性约束，对超预算支出实行事前审批与事后分析双闭环。",
    "建议推进信息系统集成，减少人工重复录入，提高数据及时性与准确性。",
    "建议常态化开展风险排查，将合规要求嵌入业务流程的关键控制点。",
]
RULES_BANK = [
    ("总则", "为规范管理、明确职责、防控风险，依据国家有关法律法规及公司实际，制定本办法。"),
    ("适用范围", "本办法适用于公司全体员工及各业务部门，控股子公司参照执行。"),
    ("管理职责", "归口管理部门负责本办法的组织落实与监督检查；各部门负责人为本部门执行第一责任人。"),
    ("执行要求", "相关人员应严格按本办法规定的流程与标准执行，不得擅自简化环节或越权审批。"),
    ("监督检查", "归口管理部门每季度组织一次专项检查，检查结果纳入部门绩效考核。"),
    ("奖励与处罚", "执行成效显著的部门和个人予以通报表扬；违反本办法的，视情节给予批评教育至纪律处分。"),
    ("附则", "本办法由归口管理部门负责解释，自发布之日起施行，原有规定与本办法不一致的以本办法为准。"),
]

GOODS = [["智慧门禁控制器", "台", f"{RNG.randrange(20, 200)}"], ["网络交换机", "台", f"{RNG.randrange(5, 60)}"],
         ["工控主机", "台", f"{RNG.randrange(3, 30)}"], ["安防摄像头", "个", f"{RNG.randrange(50, 500)}"],
         ["标签打印机", "台", f"{RNG.randrange(2, 20)}"], ["ups电源", "套", f"{RNG.randrange(2, 15)}"]]
QUOTE_ITEMS = [["需求调研与分析", "人天", "2,000"], ["系统设计与原型确认", "人天", "2,400"],
               ["软件开发与单元测试", "人天", "1,800"], ["系统集成与部署上线", "人天", "2,200"],
               ["一年期运维支持", "年", "60,000"], ["用户培训（两场）", "场", "5,000"]]


def contract_doc(kind: str, title_extra: str) -> Doc:
    clauses = CLAUSES[kind]
    d = date(2025, RNG.randrange(1, 13), RNG.randrange(1, 28))
    doc = Doc(f"{kind}{title_extra}", [f"合同编号：{no('HT')}", f"签订日期：{cn_date(d)}", f"签订地点：示例市"])
    doc.p(f"甲方（{RNG.choice(['需方', '出租方', '用人单位', '出借方', '委托方'])}）：{company()}")
    doc.p(f"乙方（{RNG.choice(['供方', '承租方', '劳动者', '借款方', '受托方'])}）：{company() if kind != '劳动合同' else person()}")
    doc.p("根据《中华人民共和国民法典》及相关法律法规，甲乙双方在平等、自愿、协商一致的基础上，订立本合同，共同遵守。")
    fills = {
        "amount": money(10, 900), "days": str(RNG.randrange(5, 31)), "date": cn_date(rand_date()),
        "date2": cn_date(rand_date(2026, 2027)), "addr": RNG.choice(ADDRESSES),
        "area": str(RNG.randrange(60, 800)), "years": str(RNG.randrange(1, 6)),
        "rent": f"{RNG.randrange(3000, 60000)}", "months": str(RNG.randrange(1, 7)),
        "dept": RNG.choice(DEPTS), "post": RNG.choice(["软件工程师", "会计", "行政专员", "销售经理", "质检员"]),
        "salary": str(RNG.randrange(4500, 25000)), "use": RNG.choice(["经营周转", "设备采购", "场地装修"]),
        "rate": str(RNG.randrange(3, 9)), "svc": RNG.choice(["信息系统运维", "客服坐席外包", "仓储装卸", "设备巡检"]),
        "rev": str(RNG.randrange(500, 9000)), "cost": str(RNG.randrange(100, 2000)),
        "rev2": str(RNG.randrange(100, 1500)), "pct": str(RNG.randrange(2, 25)),
        "pct2": str(RNG.randrange(20, 60)), "days2": str(RNG.randrange(20, 90)),
    }
    for i, (h, body) in enumerate(clauses, 1):
        doc.h(f"{'一二三四五六七八九十'[i - 1]}、{h}")
        doc.p(body.format(**fills))
    if kind == "采购合同":
        doc.h("附件：货物清单")
        doc.table(["货物名称", "单位", "数量"], [r[:] for r in RNG.sample(GOODS, 4)])
    doc.sign = [f"甲方（盖章）：{company()}", "法定代表人或授权代表（签字）：____________",
                "乙方（盖章）：________________", f"日期：{cn_date(d + timedelta(days=RNG.randrange(1, 10)))}"]
    return doc


def report_doc(title: str, intro: str, with_table: bool = False) -> Doc:
    doc = Doc(title, [f"编制单位：{company()}", f"报告日期：{cn_date(rand_date())}"])
    doc.h("一、基本情况")
    doc.p(intro)
    doc.h("二、主要情况")
    for para in RNG.sample(REPORT_FINDINGS, 4):
        doc.p(para.format(rev=str(RNG.randrange(300, 9000)), cost=str(RNG.randrange(80, 1500)),
                          rev2=str(RNG.randrange(100, 1200)), pct=str(RNG.randrange(2, 30)),
                          pct2=str(RNG.randrange(18, 65)), days=str(RNG.randrange(15, 80))))
    if with_table:
        doc.h("三、主要指标完成情况")
        rows = [[item, f"{RNG.randrange(60, 200)}", f"{RNG.randrange(50, 180)}", f"{RNG.randrange(-20, 40)}%"]
                for item in ["营业收入（万元）", "净利润（万元）", "期间费用（万元）", "研发投入（万元）"]]
        doc.table(["指标", "本年实际", "上年同期", "同比增减"], rows)
    doc.h(f"{'四' if with_table else '三'}、结论与建议")
    for para in RNG.sample(REPORT_ADVICE, 3):
        doc.p(para)
    return doc


def rules_doc(title: str) -> Doc:
    doc = Doc(title, [f"发文编号：{no('GL')}", f"发布日期：{cn_date(rand_date())}"])
    for i, (h, body) in enumerate(RULES_BANK, 1):
        doc.h(f"第{'一二三四五六七八九十'[i - 1]}条　{h}")
        doc.p(body)
    doc.sign = [f"{company()}", f"{cn_date(rand_date())}"]
    return doc


def notice_doc(title: str, body: list[str]) -> Doc:
    doc = Doc(title, [f"发文编号：{no('TW')}", f"发文日期：{cn_date(rand_date())}"])
    doc.p("公司各部门：")
    for para in body:
        doc.p(para)
    doc.p("特此通知。")
    doc.sign = [f"{company()}", f"{cn_date(rand_date())}"]
    return doc


def memo_doc() -> Doc:
    when = rand_date()
    doc = Doc("会议纪要", [f"会议时间：{cn_date(when)} 14:30—16:30", f"会议地点：公司3楼1号会议室",
                           f"主持人：{person()}", f"记录人：{person()}"])
    doc.p("出席人员：" + "、".join(person() for _ in range(6)) + "共6人。")
    doc.h("一、会议议题")
    doc.p(f"听取{RNG.choice(PROJECTS)}项目进展汇报，研究下阶段资源安排与风险应对措施。")
    doc.h("二、讨论情况")
    doc.p("项目组汇报了总体进度、已完成的里程碑和当前存在的主要问题，与会人员围绕进度偏差、人力缺口与外部依赖进行了讨论。")
    doc.p("会议认为项目总体可控，但需要重点解决跨部门协同与测试环境不足两个问题。")
    doc.h("三、会议决议")
    for i in range(1, 4):
        doc.p(f"{i}. 由{RNG.choice(DEPTS)}牵头，{RNG.choice(DEPTS)}配合，于两周内完成第{i}项决议事项的落实并书面反馈。")
    doc.sign = [f"{company()}", f"{cn_date(when)}"]
    return doc


def letter_doc(title: str, body: list[str]) -> Doc:
    doc = Doc(title)
    for para in body:
        doc.p(para)
    doc.sign = [f"{company()}", f"{cn_date(rand_date())}"]
    return doc


def quotation_doc() -> Doc:
    doc = Doc("服务报价单", [f"致：{company()}", f"报价日期：{cn_date(rand_date())}", "报价有效期：30日"])
    doc.p("根据贵方需求，我方就相关服务报价如下（含税，税率6%）：")
    doc.table(["序号", "服务项目", "计量单位", "单价（元）"],
              [[str(i), *row] for i, row in enumerate(QUOTE_ITEMS, 1)])
    doc.p("以上报价含实施与差旅费用；如需求范围发生重大变化，双方另行协商调整。")
    doc.sign = [f"{company()}", f"联系人：{person()}", f"电话：0{RNG.randrange(10, 99)}-{RNG.randrange(1000000, 9999999)}"]
    return doc


# ---------------------------------------------------------------- PDF 渲染
def render_pdf(doc: Doc, path: Path):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    fonts = Path("C:/Windows/Fonts")
    pdfmetrics.registerFont(TTFont("hei", str(fonts / "simhei.ttf")))
    pdfmetrics.registerFont(TTFont("kai", str(fonts / "simkai.ttf")))
    st_title = ParagraphStyle("t", fontName="hei", fontSize=16, alignment=1, spaceAfter=10)
    st_meta = ParagraphStyle("m", fontName="kai", fontSize=9.5, leading=14, textColor=colors.HexColor("#444444"))
    st_h = ParagraphStyle("h", fontName="hei", fontSize=11.5, spaceBefore=8, spaceAfter=4, wordWrap="CJK")
    st_p = ParagraphStyle("p", fontName="kai", fontSize=10.5, leading=17, firstLineIndent=21, wordWrap="CJK")
    st_sign = ParagraphStyle("s", fontName="kai", fontSize=10.5, leading=18, alignment=2, wordWrap="CJK")

    story = [Paragraph(doc.title, st_title), Spacer(1, 2)]
    meta = "　　".join(doc.meta)
    if meta:
        story += [Paragraph(meta, st_meta), Spacer(1, 6)]
    for item in doc.items:
        if item[0] == "h":
            story.append(Paragraph(item[1], st_h))
        elif item[0] == "p":
            story.append(Paragraph(item[1], st_p))
        else:
            _, header, rows = item
            data = [header] + rows
            widths = [170 * mm / len(header)] * len(header)
            table = Table(data, colWidths=widths)
            table.setStyle(TableStyle([
                ("FONTNAME", (0, 0), (-1, -1), "kai"), ("FONTSIZE", (0, 0), (-1, -1), 9.5),
                ("FONTNAME", (0, 0), (-1, 0), "hei"),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DEEAF6")),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#7F7F7F")),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F5F8FC")]),
            ]))
            story += [Spacer(1, 3), table, Spacer(1, 3)]
    if doc.sign:
        story += [Spacer(1, 10)] + [Paragraph(line, st_sign) for line in doc.sign]
    SimpleDocTemplate(str(path), pagesize=A4, topMargin=22 * mm, bottomMargin=20 * mm,
                      leftMargin=22 * mm, rightMargin=22 * mm,
                      title=doc.title).build(story)


# ---------------------------------------------------------------- DOCX 渲染
def render_docx(doc: Doc, path: Path):
    import docx
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor

    d = docx.Document()
    normal = d.styles["Normal"]
    normal.font.name, normal.font.size = "Calibri", Pt(10.5)
    normal._element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), "楷体")

    def para(text, size=10.5, bold=False, align=None, font="楷体", indent=True):
        p = d.add_paragraph()
        if align is not None:
            p.alignment = align
        run = p.add_run(text)
        run.font.size, run.font.bold, run.font.name = Pt(size), bold, font
        run._element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), font)
        if indent and align is None:
            p.paragraph_format.first_line_indent = Pt(21)
        return p

    para(doc.title, 16, True, WD_ALIGN_PARAGRAPH.CENTER, "黑体")
    for line in doc.meta:
        para(line, 9, False, WD_ALIGN_PARAGRAPH.CENTER, "楷体", indent=False)
    for item in doc.items:
        if item[0] == "h":
            para(item[1], 12, True, None, "黑体", indent=False)
        elif item[0] == "p":
            para(item[1])
        else:
            _, header, rows = item
            t = d.add_table(rows=len(rows) + 1, cols=len(header), style="Table Grid")
            for j, text in enumerate(header):
                cell = t.rows[0].cells[j]
                cell.text = text
                cell.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
                for run in cell.paragraphs[0].runs:
                    run.font.bold = True
            for i, row in enumerate(rows, 1):
                for j, text in enumerate(row):
                    t.rows[i].cells[j].text = text
                    t.rows[i].cells[j].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
            d.add_paragraph()
    for line in doc.sign:
        para(line, 10.5, False, WD_ALIGN_PARAGRAPH.RIGHT)
    d.save(str(path))


# ---------------------------------------------------------------- XLSX 生成
def make_xlsx(path: Path, sheets: list[tuple]):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    wb = Workbook()
    wb.remove(wb.active)
    head_fill = PatternFill("solid", fgColor="4472C4")
    head_font = Font(bold=True, color="FFFFFF", name="微软雅黑", size=10)
    body_font = Font(name="微软雅黑", size=10)
    thin = Side(style="thin", color="B0B0B0")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    for sheet_name, title, header, rows, fmts in sheets:
        ws = wb.create_sheet(sheet_name)
        if title:
            ws.append([title])
            ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(header))
            c = ws.cell(1, 1)
            c.font = Font(bold=True, size=12, name="微软雅黑")
            c.alignment = Alignment(horizontal="center", vertical="center")
            ws.row_dimensions[1].height = 22
        ws.append(header)
        for j in range(1, len(header) + 1):
            c = ws.cell(2, j)
            c.fill, c.font, c.border = head_fill, head_font, border
            c.alignment = Alignment(horizontal="center", vertical="center")
        for row in rows:
            ws.append(row)
        for row in ws.iter_rows(min_row=3, max_row=2 + len(rows), max_col=len(header)):
            for c in row:
                c.font, c.border = body_font, border
                c.alignment = Alignment(horizontal="center", vertical="center")
                if isinstance(c.value, (int, float)) and c.column <= len(fmts) and fmts[c.column - 1]:
                    c.number_format = fmts[c.column - 1]
        ws.freeze_panes = "A3"
        for j, name in enumerate(header, 1):
            width = max(len(str(name)), *(len(str(r[j - 1])) for r in rows)) if rows else len(str(name))
            ws.column_dimensions[chr(64 + j) if j <= 26 else "A"].width = min(max(width * 1.9 + 2, 9), 42)
    wb.save(str(path))


def xlsx_specs() -> list[tuple[Path, list]]:
    specs = []

    def sheet(title, header, n_rows, row_gen, fmts=()):
        rows = [row_gen(i) for i in range(1, n_rows + 1)]
        return (title[:30], title, header, rows, list(fmts))

    specs.append((Path("36_员工花名册.xlsx"), [
        sheet("员工花名册", ["工号", "姓名", "部门", "岗位", "入职日期", "联系电话"],
              25, lambda i: (f"EMP{i:04d}", person(), RNG.choice(DEPTS),
                             RNG.choice(["软件工程师", "会计", "行政专员", "销售经理", "质检员", "仓管员"]),
                             f"{RNG.randrange(2018, 2026)}-{RNG.randrange(1, 13):02d}-{RNG.randrange(1, 29):02d}",
                             f"138{RNG.randrange(10000000, 99999999)}"))]))
    specs.append((Path("37_月度销售明细表.xlsx"), [
        sheet("销售明细", ["订单号", "客户名称", "商品名称", "数量", "单价（元）", "金额（元）", "下单日期"],
              30, lambda i: (f"SO{RNG.randrange(100000, 999999)}", company(),
                             RNG.choice(["智能门禁系统", "工控主机", "网络交换机", "运维服务包", "标签打印机"]),
                             RNG.randrange(1, 50), RNG.randrange(200, 20000), None,
                             f"2026-{RNG.randrange(1, 13):02d}-{RNG.randrange(1, 29):02d}"),
              (None, None, None, "0", "#,##0", "#,##0", None))]))
    specs.append((Path("38_仓库库存清单.xlsx"), [
        sheet("库存清单", ["物料编码", "物料名称", "规格型号", "库存数量", "单位", "库位", "安全库存"],
              28, lambda i: (f"M{RNG.randrange(10000, 99999)}",
                             RNG.choice(["门禁控制器", "读卡器", "电控锁", "消防应急灯", "网线", "服务器内存条"]),
                             RNG.choice(["标准型", "增强型", "V2.0", "定制"]), RNG.randrange(0, 500),
                             RNG.choice(["台", "个", "箱", "条"]), f"{RNG.choice('ABC')}-{RNG.randrange(1, 30):02d}-{RNG.randrange(1, 6)}",
                             RNG.randrange(10, 100)))]))
    specs.append((Path("39_部门年度预算表.xlsx"), [
        sheet("预算汇总", ["部门", "年度预算（万元）", "一季度（万元）", "二季度（万元）", "三季度（万元）", "四季度（万元）"],
              8, lambda i: (dept := RNG.choice(DEPTS), RNG.randrange(20, 300), RNG.randrange(5, 80),
                            RNG.randrange(5, 80), RNG.randrange(5, 80), RNG.randrange(5, 80)),
              (None, "#,##0", "#,##0", "#,##0", "#,##0", "#,##0"))]))
    specs.append((Path("40_固定资产设备台账.xlsx"), [
        sheet("设备台账", ["资产编号", "设备名称", "品牌型号", "使用部门", "购置日期", "原值（元）", "状态"],
              24, lambda i: (f"FA{RNG.randrange(10000, 99999)}",
                             RNG.choice(["台式电脑", "笔记本", "打印机", "考勤机", "服务器", "投影仪"]),
                             RNG.choice(["A系列", "B系列", "C Pro", "D Mini"]), RNG.choice(DEPTS),
                             f"{RNG.randrange(2019, 2026)}-{RNG.randrange(1, 13):02d}-{RNG.randrange(1, 29):02d}",
                             RNG.randrange(2000, 80000), RNG.choice(["在用", "在用", "闲置", "维修中"])),
              (None, None, None, None, None, "#,##0", None))]))
    specs.append((Path("41_月度考勤统计表.xlsx"), [
        sheet("考勤统计", ["工号", "姓名", "应出勤（天）", "实出勤（天）", "迟到（次）", "早退（次）", "请假（天）", "加班（小时）"],
              22, lambda i: (f"EMP{RNG.randrange(1, 60):04d}", person(), 22, RNG.randrange(18, 23),
                             RNG.randrange(0, 4), RNG.randrange(0, 3), RNG.randrange(0, 4), RNG.randrange(0, 30)))]))
    specs.append((Path("42_客户联系名单.xlsx"), [
        sheet("客户名单", ["客户编号", "客户名称", "联系人", "职务", "电话", "邮箱", "合作状态"],
              20, lambda i: (f"C{RNG.randrange(1000, 9999)}", company(), person(),
                             RNG.choice(["采购经理", "信息部主任", "行政主管", "总经理助理"]),
                             f"139{RNG.randrange(10000000, 99999999)}",
                             f"contact{i}@example-test.cn", RNG.choice(["合作中", "合作中", "待续约", "已暂停"])))]))
    specs.append((Path("43_项目进度跟踪表.xlsx"), [
        sheet("项目进度", ["项目名称", "负责人", "开始日期", "计划完成", "进度（%）", "状态", "风险等级"],
              10, lambda i: (RNG.choice(PROJECTS), person(),
                             f"2026-{RNG.randrange(1, 11):02d}-{RNG.randrange(1, 29):02d}",
                             f"202{RNG.randrange(6, 8)}-{RNG.randrange(1, 13):02d}-{RNG.randrange(1, 29):02d}",
                             RNG.randrange(5, 100), RNG.choice(["进行中", "进行中", "已验收", "已延期"]),
                             RNG.choice(["低", "低", "中", "高"])),
              (None, None, None, None, "0", None, None))]))
    specs.append((Path("44_应收账款账龄分析表.xlsx"), [
        sheet("账龄分析", ["客户名称", "应收金额（万元）", "1个月内", "1-3个月", "3-6个月", "6个月以上", "备注"],
              15, lambda i: (company(), round(RNG.uniform(5, 300), 1), round(RNG.uniform(0, 120), 1),
                             round(RNG.uniform(0, 80), 1), round(RNG.uniform(0, 40), 1),
                             round(RNG.uniform(0, 20), 1), RNG.choice(["正常", "正常", "重点跟进", "已发函催收"])),
              (None, "0.0", "0.0", "0.0", "0.0", "0.0", None))]))
    specs.append((Path("45_采购订单汇总表.xlsx"), [
        sheet("采购订单", ["订单号", "供应商", "货物名称", "订单金额（元）", "下单日期", "到货状态"],
              26, lambda i: (f"PO{RNG.randrange(100000, 999999)}", company(),
                             RNG.choice(["智能门禁控制器", "网络交换机", "工控主机", "安防摄像头", "UPS电源"]),
                             RNG.randrange(3000, 300000), f"2026-{RNG.randrange(1, 13):02d}-{RNG.randrange(1, 29):02d}",
                             RNG.choice(["已到货", "已到货", "在途", "部分到货"])),
              (None, None, None, "#,##0", None, None))]))
    return specs


# ---------------------------------------------------------------- PPTX 生成
def make_pptx(path: Path, title: str, slides: list[tuple[str, list[str]]]):
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.util import Inches, Pt

    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)

    def add_slide(layout, title_text, bullets):
        slide = prs.slides.add_slide(layout)
        slide.shapes.title.text = title_text
        slide.shapes.title.text_frame.paragraphs[0].runs[0].font.size = Pt(32)
        if bullets:
            body = slide.placeholders[1].text_frame
            body.word_wrap = True
            for i, b in enumerate(bullets):
                p = body.paragraphs[0] if i == 0 else body.add_paragraph()
                run = p.add_run()
                run.text = b
                run.font.size = Pt(20)
                run.font.name = "微软雅黑"
        return slide

    add_slide(prs.slide_layouts[0], title, [f"汇报人：{person()}  |  {company()}"])
    for s_title, bullets in slides:
        add_slide(prs.slide_layouts[1], s_title, bullets)
    prs.save(str(path))


def pptx_specs() -> list[tuple[Path, str, list]]:
    return [
        (Path("46_季度经营分析汇报.pptx"), "季度经营分析汇报", [
            ("经营概览", ["营业收入完成年度目标的26%，符合序时进度", "主营业务毛利率稳定在31%左右", "期间费用率18.6%，同比持平"]),
            ("业务亮点", ["智慧园区项目新签合同额同比增长42%", "大客户续约率达到92%", "交付准时率提升至95%"]),
            ("问题与不足", ["部分项目回款周期偏长", "新区域市场拓展进度低于预期", "核心岗位招聘存在缺口"]),
            ("下季度计划", ["推进应收账款专项清收", "启动华南区域渠道合作", "完成组织架构与编制优化"]),
        ]),
        (Path("47_新产品发布介绍.pptx"), "星河智联V3门禁平台发布介绍", [
            ("产品定位", ["面向园区与楼宇的一体化门禁管理平台", "支持人脸、卡片、二维码多介质通行", "本地部署与云端订阅双模式"]),
            ("核心能力", ["毫秒级通行响应，峰值并发5000人次/分钟", "可视化通行报表与访客全流程管理", "开放API，可与OA、HR系统无缝对接"]),
            ("版本升级要点", ["全新算法引擎，识别准确率99.2%", "新增消防联动与应急放行策略", "管理后台全面改版，操作效率提升40%"]),
            ("服务与支持", ["7×24小时热线支持", "一年免费升级维护", "提供驻场实施服务"]),
        ]),
        (Path("48_新员工入职培训课件.pptx"), "新员工入职培训", [
            ("公司简介", ["成立于示例市，专注于智能物联领域", "下设八个一级部门，员工500余人", "企业文化：务实、协同、创新"]),
            ("制度速览", ["考勤：标准工时，弹性30分钟", "报销：费用发生当月内提交", "保密：涉密资料不得外带外传"]),
            ("信息安全须知", ["办公电脑必须安装终端安全软件", "外部邮件附件先查杀后打开", "账号密码不得共享，离开工位锁屏"]),
            ("成长路径", ["双通道：管理序列与专业序列", "入职导师制，试用期一对一带教", "内部课程平台全年开放"]),
        ]),
        (Path("49_安全生产宣导.pptx"), "安全生产宣导", [
            ("安全形势", ["本年度全公司未发生重大安全事故", "轻微工伤事件同比下降60%", "安全隐患整改完成率100%"]),
            ("常见隐患", ["消防通道堆放杂物", "临时用电私拉乱接", "设备操作未按规程佩戴防护"]),
            ("应对措施", ["每月安全巡查与随机抽查相结合", "隐患随手拍，上报即奖励", "重点岗位持证上岗，年度复训"]),
            ("应急处置", ["火警：就近按下报警按钮并有序疏散", "急救：第一时间拨打急救电话并报告上级", "演习：每半年一次全员疏散演练"]),
        ]),
        (Path("50_项目复盘总结.pptx"), f"{RNG.choice(PROJECTS)}项目复盘总结", [
            ("项目概况", ["项目周期：10个月，按期完成验收", "投入人力：研发、测试、实施共18人", "交付物：平台软件、接口文档、操作手册"]),
            ("目标达成", ["功能交付率100%，验收一次通过", "上线以来系统可用率99.9%", "用户满意度评分4.6/5.0"]),
            ("经验教训", ["需求冻结点需前移，变更成本过高", "跨部门接口联调应更早介入", "测试环境与生产环境配置差异要建立基线"]),
            ("后续行动", ["沉淀通用组件，复用率目标50%", "建立项目模板与检查清单", "开展内部技术分享与复盘会"]),
        ]),
    ]


# ---------------------------------------------------------------- 组装 50 份
def doc_specs() -> list[tuple[str, Doc]]:
    return [
        ("01_采购合同.pdf", contract_doc("采购合同", "")),
        ("02_房屋租赁合同.pdf", contract_doc("房屋租赁合同", "")),
        ("03_劳动合同.pdf", contract_doc("劳动合同", "")),
        ("04_保密协议.pdf", contract_doc("保密协议", "")),
        ("05_借款协议.pdf", contract_doc("借款协议", "")),
        ("06_服务外包合同.pdf", contract_doc("服务外包合同", "")),
        ("07_项目验收报告.pdf", report_doc("项目验收报告",
            f"受建设单位委托，我司对{RNG.choice(PROJECTS)}项目组织验收。项目自2025年启动，已完成合同约定的全部建设内容，本次验收范围为功能实现、性能指标与文档交付三部分。", True)),
        ("08_年度财务分析报告.pdf", report_doc("年度财务分析报告",
            "本报告依据公司年度财务报表及经营数据编制，对收入、成本、费用、现金流及主要财务指标进行分析，为经营决策提供参考。", True)),
        ("09_可行性研究报告.pdf", report_doc("可行性研究报告",
            f"为满足业务发展需要，公司拟实施{RNG.choice(PROJECTS)}项目。本报告从业务需求、技术方案、投资估算与效益预测等方面论证项目可行性。", False)),
        ("10_年度审计报告.pdf", report_doc("年度审计报告",
            "我们接受委托，对公司年度财务报表进行了审计。审计范围包括资产负债表、利润表、现金流量表及财务报表附注。", True)),
        ("11_风险评估报告.pdf", report_doc("风险评估报告",
            "本报告对公司面临的市场、运营、财务与合规四类主要风险进行识别与评估，评估采用定性分析与关键指标量化相结合的方法。", False)),
        ("12_市场调研报告.pdf", report_doc("市场调研报告",
            "本次调研覆盖华东、华南两个重点区域，采取问卷调查与客户访谈相结合的方式，共回收有效问卷412份，深度访谈客户16家。", False)),
        ("13_会议纪要.pdf", memo_doc()),
        ("14_员工差旅费管理办法.pdf", rules_doc("员工差旅费管理办法")),
        ("15_安全生产管理制度.pdf", rules_doc("安全生产管理制度")),
        ("16_产品使用说明书.pdf", Doc("星河智联V3门禁平台使用说明书")),
        ("17_服务报价单.pdf", quotation_doc()),
        ("18_授权委托书.pdf", letter_doc("授权委托书", [
            f"委托人：{company()}；受托人：{person()}，身份证号：999901198501120011（虚构）。",
            f"现委托受托人代表委托人办理{RNG.choice(['工商变更登记', '项目投标事宜', '合同签署事宜'])}相关事项，委托权限为特别授权。",
            "受托人在授权范围内实施的行为，委托人均予以承认。委托期限自签署之日起至事项办结之日止。"])),
        ("19_离职证明.pdf", letter_doc("离职证明", [
            f"兹证明{person()}（身份证号：999901199203150026，虚构）自2022年3月至2026年5月在我公司{RNG.choice(DEPTS)}工作，历任专员、主管职务。",
            "该员工因个人原因提出离职，双方已于2026年5月31日解除劳动合同，相关工作交接已办理完毕。",
            "该员工在职期间无违规违纪记录。特此证明。本证明仅用于证明离职事实，不作担保用途。"])),
        ("20_采购询价函.pdf", letter_doc("采购询价函", [
            f"我司拟采购以下设备：{ '、'.join(r[0] for r in RNG.sample(GOODS, 3)) }，具体数量与技术要求见附件。",
            "请贵司于收函后5个工作日内书面回复报价（含税、含运费），并注明供货周期与质保期限。",
            "本次询价不构成采购承诺，我司将根据报价、资质与服务能力综合确定合作方。"])),
        ("21_年度资产盘点通知.docx", notice_doc("关于开展年度资产盘点工作的通知", [
            "为摸清公司资产家底，加强实物资产管理，经研究决定于2026年12月开展年度资产盘点工作。",
            f"本次盘点范围为公司全部固定资产与低值易耗品，由{RNG.choice(DEPTS)}牵头，各部门指定专人配合，盘点基准日为2026年11月30日。",
            "各部门请于12月5日前完成自查并报送《资产自查表》，12月10日起盘点小组进行现场复核。",
            "盘点中发现盘盈、盘亏或毁损的，应查明原因、明确责任，并按公司资产管理制度办理审批手续。"])),
        ("22_消防安全培训通知.docx", notice_doc("关于组织消防安全培训的通知", [
            "为进一步增强员工消防安全意识，提高火灾防范与应急处置能力，公司决定组织开展消防安全专题培训。",
            f"培训时间：2026年12月9日（周三）下午14:00；培训地点：公司一楼多功能厅；培训对象：全体员工。",
            f"培训内容包括消防法律法规解读、典型火灾案例分析、灭火器材实操演练与疏散逃生要点，由{person()}讲师主讲。",
            "请各部门合理安排工作，组织员工准时参加，参训情况将纳入部门安全考核。"])),
        ("23_信息系统上线公告.docx", notice_doc("关于新版办公OA系统上线切换的公告", [
            "为提升办公效率，公司已完成新版办公OA系统的建设与测试，定于2026年12月18日正式上线切换。",
            "切换期间旧系统将于12月17日18:00停止受理新流程，未办结事项由各部门导出留档，12月19日起全部业务在新系统办理。",
            "新系统账号沿用工号，初始密码将通过企业微信下发，首次登录后须修改密码并完成手机号绑定。",
            f"系统使用手册已发布至共享盘，如有问题请联系{RNG.choice(DEPTS)}，服务分机8001。"])),
        ("24_部门季度工作总结.docx", report_doc(f"{RNG.choice(DEPTS)}季度工作总结",
            "本季度部门围绕年度重点任务，扎实推进各项工作，在项目交付、流程优化与团队建设方面取得阶段性成效。现将主要情况总结如下。", True)),
        ("25_客户投诉处理报告.docx", report_doc("客户投诉处理报告",
            f"2026年10月，公司客服中心收到客户{person()}关于{RNG.choice(['门禁设备偶发失灵', '发票开具延迟', '售后响应超时'])}的投诉（工单号TS{RNG.randrange(1000, 9999)}）。客服中心会同相关部门进行调查处理，现将情况报告如下。", False)),
        ("26_供应商准入评估报告.docx", report_doc("供应商准入评估报告",
            f"根据采购管理制度，采购部组织对{company()}开展准入评估，评估组由采购、质量、财务三部门人员组成，通过资料审查、现场考察与样品测试三种方式实施。", False)),
        ("27_事故调查报告.docx", report_doc("事故调查报告",
            f"2026年10月{RNG.randrange(10, 29)}日上午，公司仓储部发生一起叉车碰撞货架事件，造成货架局部变形，无人员伤亡。事故发生后，公司立即成立调查组开展调查，现将调查情况报告如下。", False)),
        ("28_项目实施方案.docx", report_doc(f"{RNG.choice(PROJECTS)}项目实施方案",
            "本方案明确项目的建设目标、总体架构、实施路径、组织保障与进度安排，作为项目执行与考核的依据。", True)),
        ("29_年会活动策划方案.docx", report_doc("2027年公司年会活动策划方案",
            "为总结年度经营成果、表彰先进、增强团队凝聚力，拟于2027年1月举办公司年会。本方案就活动主题、流程安排、预算与分工进行策划。", True)),
        ("30_设备年度维护计划.docx", report_doc("生产设备年度维护保养计划",
            "为保障设备安全稳定运行，降低故障停机风险，依据设备管理制度与制造商维保建议，编制本年度维护保养计划。", True)),
        ("31_招聘启事.docx", notice_doc("招聘启事", [
            f"{company()}因业务发展需要，现面向社会公开招聘以下岗位人员。",
            f"岗位一：{RNG.choice(['嵌入式软件工程师', '大数据开发工程师', '解决方案顾问'])}2名，要求本科及以上学历，三年以上相关工作经验，熟悉主流开发框架。",
            f"岗位二：{RNG.choice(['行政前台', '会计主管', '质检工程师'])}1名，要求大专及以上学历，工作细致、责任心强。",
            "薪酬面议，五险一金、带薪年假、节日福利齐全。有意者请将简历发送至hr@example-test.cn，邮件标题注明应聘岗位。"])),
        ("32_加班与调休管理办法.docx", rules_doc("加班与调休管理办法")),
        ("33_内部审计整改通知书.docx", notice_doc("关于内部审计发现问题整改的通知", [
            f"根据公司年度内部审计计划，审计组于近期对{RNG.choice(DEPTS)}开展了专项审计，发现合同管理、费用报销等方面存在待完善事项。",
            "现将《审计发现问题清单》随文下发，请责任部门逐项分析原因，制定整改措施，明确责任人与完成时限。",
            "请于收到本通知后20个工作日内报送整改报告及佐证材料，审计组将适时组织整改效果复查。"])),
        ("34_合作备忘录.docx", memo_doc()),
        ("35_团建活动方案.docx", report_doc("秋季团建活动方案",
            "为丰富员工业余生活、增进跨部门交流，拟于11月组织秋季团建活动。本方案就活动时间、地点、项目安排、费用预算与安全预案进行说明。", True)),
    ]


def manual_doc_fix(doc: Doc):
    """16_产品使用说明书 单独补充内容。"""
    doc.meta = [f"{company()}", f"版本：V3.{RNG.randrange(1, 9)}", f"发布日期：{cn_date(rand_date())}"]
    doc.h("一、产品概述")
    doc.p("星河智联V3门禁平台是面向园区与楼宇的一体化通行管理系统，支持人脸、卡片、二维码多种通行介质，提供设备管理、权限管理、通行记录与访客管理等功能。")
    doc.h("二、安装要求")
    doc.p("服务器配置不低于8核CPU、16GB内存、500GB硬盘；浏览器推荐使用最新版Chrome或Edge；网络环境需保证设备与管理平台之间双向可达。")
    doc.h("三、初始配置步骤")
    doc.p("1. 登录管理后台，首次使用默认管理员账号并立即修改密码；2. 在【设备管理】中添加门禁控制器并配置所在区域；3. 在【权限管理】中建立人员组织树并下发通行权限；4. 在【系统设置】中配置备份策略与告警通知方式。")
    doc.h("四、常见问题处理")
    doc.p("设备离线时请先检查网络与电源；识别失败率明显升高时，请重新采集人脸照片并保证光照充足；数据异常时可使用【数据修复】工具进行自检。")
    doc.h("五、安全注意事项")
    doc.p("平台账号不得共享，应定期更换密码；导出的通行记录包含个人信息，须按公司数据安全规定存放与销毁；固件升级前请完整备份数据。")
    doc.sign = [f"{company()}", "本说明书内容如有更新，恕不另行通知"]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    counts = {"pdf": 0, "docx": 0, "xlsx": 0, "pptx": 0}
    for filename, doc_model in doc_specs():
        if filename == "16_产品使用说明书.pdf":
            manual_doc_fix(doc_model)
        path = OUT / filename
        if filename.endswith(".pdf"):
            render_pdf(doc_model, path)
            counts["pdf"] += 1
        else:
            render_docx(doc_model, path)
            counts["docx"] += 1
        print(filename)
    for path, sheets in xlsx_specs():
        make_xlsx(path if path.is_absolute() else OUT / path, sheets)
        counts["xlsx"] += 1
        print(path.name)
    for path, title, slides in pptx_specs():
        make_pptx(OUT / path, title, slides)
        counts["pptx"] += 1
        print(path.name)
    total = sum(counts.values())
    print(f"\n共 {total} 份 → {OUT}")
    print("格式分布：", counts)
    assert total == 50, counts
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
