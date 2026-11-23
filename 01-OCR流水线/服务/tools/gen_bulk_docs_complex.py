"""批量生成 50 份"复杂长文档"测试集（PDF/DOCX/XLSX/PPTX，全部虚构内容，固定种子）。

与 gen_bulk_docs.py 的区别：每份文档显著加长加复杂——
  PDF   2-4 页：合同带 10+ 条款与通用条款、两个附件表；报告 6 章 2 表；
  DOCX  3-5 页：真实 Heading 多级标题结构、页脚页码域、表格与编号列表；
  XLSX  多工作表：明细页 40-60 行 + 汇总页（SUM 公式）+ 维度页；
  PPTX  12 页以上：封面/目录/分章节内容页/数据表页/总结页。

用法：
    python tools/gen_bulk_docs_complex.py <输出目录> [--seed 20261124]
"""
from __future__ import annotations

import argparse
import random
import sys
from datetime import date, timedelta
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("out", nargs="?", type=Path,
                    default=Path("C:/Users/Wren-GS/Desktop/OCR测试集6/02_非图片文档"))
parser.add_argument("--seed", type=int, default=20261124)
ARGS = parser.parse_args()
RNG = random.Random(ARGS.seed)
OUT = ARGS.out

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
POSTS = ["软件工程师", "会计", "行政专员", "销售经理", "质检员", "仓管员", "运维工程师", "产品经理"]


def person():
    return RNG.choice(SURNAMES) + RNG.choice(GIVEN)


def company():
    return RNG.choice(COMPANIES)


def cn_date(d: date) -> str:
    return f"{d.year}年{d.month}月{d.day}日"


def rand_date(y0=2024, y1=2026):
    return date(RNG.randrange(y0, y1 + 1), RNG.randrange(1, 13), RNG.randrange(1, 29))


def doc_no(prefix="TW"):
    return f"{prefix}〔{RNG.randrange(2024, 2027)}〕{RNG.randrange(1, 200):03d}号"


def money_w(low=10, high=900):
    return f"{RNG.randrange(low, high)}万元"


# ---------------------------------------------------------------- 通用素材库
GENERIC_CLAUSES = [
    ("双方权利与义务", "甲方应按约定及时提供必要的场地、条件与资料，并按约定支付款项；乙方应按约定的标准、进度与质量完成合同义务，并自行承担履行过程中的人员与管理责任。双方应各指定一名项目联络人，负责日常沟通与事项协调。"),
    ("税费承担", "因履行本合同发生的各项税费，由双方按法律规定各自承担。乙方收取款项前应向甲方开具合法有效的增值税发票，发票类型与税率以合同附件载明为准；发票不符合约定的，甲方有权要求换开并顺延付款期限。"),
    ("廉洁合作", "双方及其工作人员不得以任何形式向对方人员提供回扣、礼金或其他不正当利益。违反本条约定的，守约方有权解除合同，并要求违约方赔偿由此造成的全部损失。"),
    ("通知与送达", "双方因履行本合同发出的通知，应以书面形式送达对方在首部载明的地址或双方书面确认的电子邮箱。以专人送达的，签收之日视为送达；以邮寄方式的，交寄后第三个工作日视为送达；以电子邮件方式的，发送成功之日视为送达。一方变更地址未书面通知对方的，按原地址送达仍视为有效送达。"),
    ("不可抗力", "因地震、洪水、战争、政府行为等不能预见、不能避免且不能克服的不可抗力事件，致使一方不能履行或不能完全履行本合同的，遭受不可抗力的一方应在事件发生后七日内书面通知对方，并在合理期限内提供有关证明。根据不可抗力的影响程度，双方协商变更或解除本合同，遭受不可抗力的一方在通知义务范围内免责。"),
    ("数据保护", "乙方在履约过程中处理甲方数据的，应遵循最小必要原则，仅用于履约目的，并采取加密、访问控制等必要安全措施。履约结束后，乙方应按甲方要求返还或销毁相关数据，并出具书面确认。"),
    ("保密条款", "任何一方在合同履行过程中知悉的对方商业秘密、技术资料、经营数据与客户信息，未经对方书面同意不得向任何第三方披露，亦不得用于本合同目的之外的使用。保密义务在本合同终止后两年内持续有效。"),
    ("合同变更与转让", "本合同的任何变更、补充均须以书面形式作出，经双方签字盖章后与本合同具有同等法律效力。未经对方书面同意，任何一方不得将本合同项下的权利义务全部或部分转让给第三方。"),
    ("合同解除", "经双方协商一致，可以解除本合同。一方存在根本违约情形的，守约方有权书面通知解除合同，并要求违约方赔偿损失。合同解除后，双方应按实结算已履行部分，并做好资料与现场交接。"),
    ("售后服务", "质保期或服务期内，乙方应对合同标的存在的质量缺陷免费维修或更换。乙方应在接到通知后四小时内响应，一般故障二十四小时内解决；不能按时解决的，应提供备用方案。"),
    ("争议解决", "因本合同引起的或与本合同有关的任何争议，双方应首先友好协商解决；协商不成的，任何一方均可向合同签订地有管辖权的人民法院提起诉讼。争议解决期间，除争议事项外，双方应继续履行本合同的其他条款。"),
    ("合同的生效与份数", "本合同自双方法定代表人或授权代表签字并加盖公章之日起生效。本合同一式肆份，甲乙双方各执贰份，具有同等法律效力。本合同附件为本合同不可分割的组成部分。"),
]


def cn_num(i: int) -> str:
    """1→一 ... 10→十 11→十一 12→十二 ... 20→二十"""
    nums = "一二三四五六七八九"
    if i <= 0:
        return str(i)
    if i < 10:
        return nums[i - 1]
    if i == 10:
        return "十"
    if i < 20:
        return "十" + nums[i % 10 - 1]
    return "二十" + (nums[i % 10 - 1] if i % 10 else "")
FINDINGS = [
    "报告期内营业收入{rev}万元，同比增长{pct}%，其中主营业务收入占比稳定在八成以上，收入结构总体健康。",
    "期间费用合计{cost}万元，费用率与上年基本持平，其中销售费用受市场活动节奏影响呈现前低后高的分布特征。",
    "应收账款余额{rev2}万元，账龄一年以内的占比约{pct2}%，回款情况总体可控，长账龄客户已纳入重点清收范围。",
    "存货周转天数约{days}天，较上年同期缩短，供应链协同效率有所提升，但部分物料仍存在积压。",
    "研发投入{rd}万元，占营业收入比重{pct3}%，投入强度处于行业中等偏上水平。",
    "报告期内未发生重大安全环保事故与重大合规风险事件，内控制度运行总体有效。",
    "主要客户与供应商结构保持稳定，前五大客户收入占比{pct2}%，客户集中度处于合理区间，未出现单一客户依赖。",
    "现金流状况总体平稳，经营活动现金流量净额为{cash}万元，与净利润的比值处于健康水平。",
]
FINDINGS2 = [
    "报告期内共完成重点项目{p_num}个，其中按期交付{p_ok}个，按期交付率{pct}%，交付质量总体稳定。",
    "客户满意度得分为{sati}分（满分百分制），较上期提高{sati_up}分，投诉处理及时率达到{pct2}%。",
    "人均效能持续改善，人均营业收入较上年增长{pct3}%，人员结构与业务结构匹配度进一步提高。",
    "渠道方面，新增合作渠道{ch}家，渠道贡献收入占比提升至{pct}%，渠道结构更趋均衡。",
    "报告期内完成流程优化事项{p_num}项，平均审批环节减少{days}个，流程运转效率明显提升。",
    "库存结构分析显示，呆滞物料占存货比重为{pct3}%，较上年下降，但部分品类仍需专项处置。",
    "培训与发展方面，累计开展内部培训{p_num}场，覆盖员工{pct2}人次，关键岗位持证率{pct}%。",
    "信息化投入持续发挥效益，主要业务系统月活跃用户占比{pct}%，线上办理事项占比{pct2}%。",
]
ADVICE = [
    "建议持续优化产品与业务结构，加大高毛利业务的资源投入，提升整体盈利能力。",
    "建议加强应收账款过程管理，对长账龄客户实行专人跟进与信用限额管控。",
    "建议完善预算刚性约束机制，对超预算支出实行事前审批与事后分析双闭环管理。",
    "建议推进信息系统深度集成，减少人工重复录入，提高数据及时性与准确性。",
    "建议常态化开展风险排查，将合规要求嵌入业务流程的关键控制点。",
    "建议强化人才梯队建设，针对关键岗位制定继任者计划与轮岗安排。",
]
RULES_COMMON = [
    ("总则", "为规范管理、明确职责、防控风险，依据国家有关法律法规及公司实际，制定本办法。"),
    ("适用范围", "本办法适用于公司全体员工及各业务部门，控股子公司参照执行。"),
    ("管理职责", "归口管理部门负责本办法的组织落实与监督检查；各部门负责人为本部门执行第一责任人。"),
    ("执行要求", "相关人员应严格按本办法规定的流程与标准执行，不得擅自简化环节或越权审批。"),
    ("监督检查", "归口管理部门每季度组织一次专项检查，检查结果纳入部门绩效考核。"),
    ("记录与档案", "本办法执行过程中形成的审批单、台账与检查记录，由归口管理部门统一归档，保存期限不少于五年，作为考核、审计与责任追究的依据。"),
    ("费用与预算", "因执行本办法发生的费用，纳入归口管理部门年度预算，按公司预算管理制度执行；超预算支出须履行追加审批程序。"),
    ("奖励与处罚", "执行成效显著的部门和个人予以通报表扬；违反本办法的，视情节给予批评教育至纪律处分。"),
    ("附则", "本办法由归口管理部门负责解释，自发布之日起施行，原有规定与本办法不一致的以本办法为准。"),
]
GOODS = [["智慧门禁控制器", "台"], ["网络交换机", "台"], ["工控主机", "台"],
         ["安防摄像头", "个"], ["标签打印机", "台"], ["UPS电源", "套"],
         ["电子秤", "台"], ["手持终端", "台"]]


def goods_rows(n=5):
    return [[name, unit, str(RNG.randrange(2, 120)), f"{RNG.randrange(500, 30000)}"]
            for name, unit in RNG.sample(GOODS, n)]


# ---------------------------------------------------------------- 文档模型（PDF/DOCX 共用）
class Doc:
    def __init__(self, title, meta=None):
        self.title, self.meta, self.items, self.sign = title, meta or [], [], []

    def h(self, t):
        self.items.append(("h", t))

    def p(self, t):
        self.items.append(("p", t))

    def table(self, header, rows):
        self.items.append(("table", header, rows))


def contract_doc(kind, extra_clauses, annex_a=None, annex_b=None, annex_c=None):
    d = rand_date(2025, 2026)
    doc = Doc(f"{kind}", [f"合同编号：{doc_no('HT')}", f"签订日期：{cn_date(d)}", "签订地点：示例市"])
    party_a, party_b = company(), company()
    doc.p(f"甲方（委托方）：{party_a}")
    doc.p(f"乙方（受托方）：{party_b}")
    doc.p("根据《中华人民共和国民法典》及相关法律法规，甲乙双方在平等、自愿、协商一致的基础上，就有关事项订立本合同，共同遵守。")
    fills = dict(amount=money_w(50, 1200), date=cn_date(rand_date()), date2=cn_date(rand_date(2026, 2028)),
                 addr=RNG.choice(ADDRESSES), company=party_b, dept=RNG.choice(DEPTS),
                 months=str(RNG.randrange(3, 25)), days=str(RNG.randrange(5, 30)))
    clauses = list(extra_clauses) + GENERIC_CLAUSES
    for i, (h, body) in enumerate(clauses, 1):
        doc.h(f"{cn_num(i)}、{h}")
        doc.p(body.format(**fills) if "{" in body else body)
    if annex_a:
        doc.h("附件一：" + annex_a[0])
        doc.table(annex_a[1], annex_a[2])
        doc.p(annex_a[3] if len(annex_a) > 3 else "附件与正文具有同等法律效力，附件与正文不一致的，以正文为准。")
    if annex_b:
        doc.h("附件二：" + annex_b[0])
        doc.table(annex_b[1], annex_b[2])
    if annex_c:
        doc.h("附件三：" + annex_c[0])
        doc.table(annex_c[1], annex_c[2])
    doc.sign = [f"甲方（盖章）：{party_a}", "法定代表人或授权代表（签字）：____________",
                "乙方（盖章）：" + party_b, f"日期：{cn_date(d + timedelta(days=RNG.randrange(1, 15)))}"]
    return doc


CONTRACT_TYPES = [
    ("采购合同", [
        ("标的与数量", "乙方向甲方供应本合同附件一所列货物，货物的名称、规格、数量及单价以附件为准，总价合计人民币{amount}（含税）。"),
        ("质量标准", "货物质量应符合国家标准及行业通行标准，并随货提供出厂合格证、检测报告与质保承诺书。质保期自验收合格之日起不少于十二个月。"),
        ("价款与支付", "合同签订后七个工作日内，甲方向乙方支付合同总价的百分之三十作为预付款；货物全部到货且初验合格后支付至百分之六十；终验合格后支付至百分之九十五；余款作为质保金，质保期满无质量问题后十个工作日内无息付清。"),
        ("交付与运输", "乙方应于{date}前将货物运抵甲方指定地点，运输费用与风险由乙方承担至甲方签收之时。甲方应在到货后七个工作日内完成验收，验收不合格的，乙方应在十个工作日内更换或修复，费用自负。"),
        ("包装要求", "货物包装应符合防潮、防震、防腐蚀及长途运输要求，因包装不善造成的损坏由乙方负责。包装物不另行计价，随货提供。"),
        ("违约责任", "乙方逾期交付的，每逾期一日按合同总价的千分之零点五支付违约金，累计不超过合同总价的百分之十；货物质量不符合约定的，甲方有权拒收并要求退货退款，乙方并应赔偿甲方由此遭受的直接损失。甲方逾期付款的，每逾期一日按应付未付金额的千分之零点五支付违约金。"),
    ]),
    ("房屋租赁合同", [
        ("租赁标的", "甲方将位于{addr}的房屋出租给乙方使用，建筑面积约二百四十平方米，房屋权属清晰，无抵押、查封等权利限制。"),
        ("租赁期限与用途", "租赁期自{date}起至{date2}止。房屋用途为办公，乙方不得擅自改变房屋用途或转租。"),
        ("租金与支付", "月租金人民币贰万捌仟元整，按季度支付，乙方应于每季度首月五日前支付当季租金。逾期支付的，每逾期一日按月租金的百分之一支付滞纳金。"),
        ("押金", "乙方应于签约时向甲方支付相当于两个月租金的押金。租赁期满且乙方结清全部费用、房屋无损坏的，甲方应在交还房屋后十个工作日内无息退还押金。"),
        ("维修与装修", "房屋主体结构与固定设施由甲方负责维修；乙方使用不当造成的损坏由乙方负责修复或赔偿。乙方装修须经甲方书面同意，退租时按恢复原状或甲方认可的现状移交。"),
        ("合同解除", "任何一方提前解除合同的，应提前三十日书面通知对方，并按一个月租金的标准支付违约金；因不可抗力或政府征收导致合同无法履行的，双方均不承担违约责任。"),
    ]),
    ("技术开发合同", [
        ("开发内容", "乙方接受甲方委托，开发{company}业务管理系统，开发范围包括需求分析、系统设计、编码实现、测试上线与为期一年的维护，具体以附件一《开发任务书》为准。"),
        ("开发费用", "开发费用合计人民币{amount}（含税），按里程碑分期支付：合同生效后支付百分之二十，系统设计确认后支付百分之二十，开发完成后支付百分之三十，验收合格后支付百分之二十，余款于维护期满后支付。"),
        ("交付与验收", "乙方应于{date}前完成全部开发内容并提交验收申请。甲方应在收到申请后二十个工作日内组织验收；验收未通过的，乙方应在三十日内完成整改并再次提请验收。"),
        ("知识产权", "本项目开发成果的知识产权归甲方所有。乙方保证交付成果不侵犯任何第三方权利，因侵权引发纠纷的，由乙方负责处理并承担全部责任。"),
        ("人员与保密", "乙方应指定项目负责人并保持核心人员稳定，核心人员变更须提前十个工作日书面通知甲方。开发过程中接触的甲方数据按双方签署的保密协议执行。"),
        ("违约责任", "乙方逾期交付的，每逾期一日按开发费用的千分之一支付违约金；甲方逾期付款的，每逾期一日按应付金额的千分之一支付违约金，违约金累计均不超过合同总价的百分之十五。"),
    ]),
    ("劳动合同", [
        ("合同期限", "本合同为固定期限劳动合同，自{date}起至{date2}止，其中试用期{months}个月。"),
        ("工作内容与地点", "甲方聘用乙方在{dept}担任相应岗位工作，工作地点为示例市。甲方可根据经营需要及乙方工作能力，依法合理调整乙方的工作岗位与工作地点。"),
        ("劳动报酬", "乙方月工资由基本工资、岗位工资与绩效工资构成，合计不低于当地最低工资标准，于每月十五日前发放上一自然月工资。试用期工资为转正后工资的百分之八十。"),
        ("工作时间与休假", "甲方实行标准工时制度，每日工作八小时，每周工作四十小时。乙方依法享受法定节假日、带薪年休假、婚丧假、产假等假期。"),
        ("社会保险与福利", "甲方依法为乙方缴纳养老、医疗、失业、工伤、生育等社会保险及住房公积金，个人承担部分由甲方从乙方工资中代扣代缴。"),
        ("合同的解除与终止", "双方协商一致可以解除本合同；符合《中华人民共和国劳动合同法》规定情形的，按法律规定执行。乙方解除本合同应提前三十日书面通知甲方，试用期内提前三日通知。"),
    ]),
    ("服务外包合同", [
        ("服务内容", "乙方为甲方提供信息系统运维服务，服务范围包括桌面运维、机房巡检、系统监控与故障处置，服务标准与考核细则以附件为准。"),
        ("服务期限", "服务期自{date}起至{date2}止。服务期满前两个月，双方可协商续约事宜。"),
        ("服务费用与结算", "服务费合计人民币{amount}，按季度结算。乙方于每季度首月开具上季度增值税专用发票，甲方在收到发票后二十个工作日内支付。"),
        ("服务标准与考核", "乙方应保证服务台工作时间接通率不低于百分之九十五，故障响应时间不超过三十分钟，一般故障四小时内解决。月度考核得分低于八十分的，按比例扣减当季服务费。"),
        ("人员管理", "乙方派驻人员应遵守甲方现场管理制度，佩戴出入证件。派驻人员与乙方建立劳动关系，其工资、社会保险由乙方负责，与甲方不存在劳动关系。"),
    ]),
    ("借款协议", [
        ("借款金额与用途", "甲方向乙方出借人民币{amount}，用于乙方经营周转。乙方应按约定用途使用借款，不得挪作他用。"),
        ("借款期限", "借款期限自{date}起至{date2}止，共{months}个月。借款到期后一次性还本付息。"),
        ("利息", "借款利率为年利率百分之六点五，自实际放款之日起按实际占用天数计息。乙方提前还款的，按实际占用天数结算利息，不收取违约金。"),
        ("还款方式", "借款到期日，乙方应将本金与利息一次性汇入甲方指定账户。乙方逾期还款的，按每日万分之五支付逾期利息，并承担甲方实现债权的合理费用。"),
    ]),
    ("软件许可合同", [
        ("许可内容", "乙方将其拥有合法权利的企业管理软件许可甲方使用，许可范围为甲方总部及下属两个分支机构，许可用户数不超过五百个，软件清单以附件为准。"),
        ("许可费用", "许可费用合计人民币{amount}，含首年维护费。合同生效后十个工作日内支付百分之五十，软件部署验收合格后支付剩余款项。次年起维护费按许可费的百分之十五逐年收取。"),
        ("交付与部署", "乙方应于{date}前完成软件交付与部署，并提供不少于两场的用户培训。部署环境由甲方提供，乙方应提前书面告知环境要求。"),
        ("许可限制", "未经乙方书面同意，甲方不得将软件转让、转授权或超出许可范围使用，不得对软件进行反向工程。甲方备份数据的权利不受限制。"),
    ]),
]


def _fills():
    return dict(rev=str(RNG.randrange(300, 9000)), cost=str(RNG.randrange(80, 1800)),
                rev2=str(RNG.randrange(80, 1300)), pct=str(RNG.randrange(2, 30)),
                pct2=str(RNG.randrange(18, 60)), days=str(RNG.randrange(15, 90)),
                rd=str(RNG.randrange(50, 900)), pct3=str(RNG.randrange(2, 9)),
                cash=str(RNG.randrange(-100, 1500)), p_num=str(RNG.randrange(6, 40)),
                p_ok=str(RNG.randrange(4, 36)), sati=str(RNG.randrange(78, 96)),
                sati_up=str(RNG.randrange(1, 8)), ch=str(RNG.randrange(3, 30)))


def report_doc(title, intro, chapter_titles, tables=None, with_advice=True):
    """多章节报告：摘要 + 概况 + 数据章节（双素材库） + 表格 + 结论建议。"""
    doc = Doc(title, [f"编制单位：{company()}", f"报告日期：{cn_date(rand_date())}", f"文件编号：{doc_no('BG')}"])
    doc.h("一、摘要")
    doc.p(intro)
    doc.h("二、背景与基本情况")
    for para in RNG.sample(FINDINGS, 5):
        doc.p(para.format(**_fills()))
    bank = RNG.sample(FINDINGS, 4) + RNG.sample(FINDINGS2, 4)
    idx = 3
    for chap in chapter_titles:
        doc.h(f"{cn_num(idx)}、{chap}")
        for para in bank[:4]:
            doc.p(para.format(**_fills()))
        bank = bank[4:] + RNG.sample(FINDINGS + FINDINGS2, 4)
        if tables:
            header, rows = tables.pop(0)
            doc.p("有关明细数据如下表所示：")
            doc.table(header, rows)
        idx += 1
    if with_advice:
        doc.h(f"{cn_num(idx)}、结论与建议")
        doc.p("综合以上分析，总体判断是：主要经营指标处于合理区间，结构性与管理性问题并存，需要通过持续的结构优化与精细化管控加以解决。")
        for para in RNG.sample(ADVICE, 5):
            doc.p(para)
    doc.sign = [company(), cn_date(rand_date())]
    return doc


def rules_doc(title, extra_rules):
    doc = Doc(title, [f"发文编号：{doc_no('GL')}", f"发布日期：{cn_date(rand_date())}", "密级：内部公开"])
    rules = [("第{}条　" + h, b) for (h, b) in RULES_COMMON]
    # 展开为 编号+标题 形式
    cn_seq = "一二三四五六七八九"  # noqa: F841（保留序号字表备用）
    all_rules = RULES_COMMON[:2] + extra_rules + RULES_COMMON[2:]
    for i, (h, body) in enumerate(all_rules, 1):
        doc.h(f"第{cn_num(i)}条　{h}")
        doc.p(body)
    doc.sign = [company(), cn_date(rand_date())]
    return doc


def report_tables():
    t1 = (["指标", "本期实际", "上期同期", "增减幅度"],
          [[item, f"{RNG.randrange(60, 900)}", f"{RNG.randrange(50, 800)}", f"{RNG.randrange(-25, 40)}%"]
           for item in ["营业收入（万元）", "净利润（万元）", "期间费用（万元）", "研发投入（万元）", "经营现金流净额（万元）"]])
    t2 = (["部门", "计划完成率", "实际完成率", "考核得分"],
          [[d, f"{RNG.randrange(80, 100)}%", f"{RNG.randrange(70, 105)}%", str(RNG.randrange(65, 98))]
           for d in RNG.sample(DEPTS, 5)])
    return [t1, t2]


# ---------------------------------------------------------------- PDF 渲染（多页）
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
    if doc.meta:
        story += [Paragraph("　　".join(doc.meta), st_meta), Spacer(1, 6)]
    for item in doc.items:
        if item[0] == "h":
            story.append(Paragraph(item[1], st_h))
        elif item[0] == "p":
            story.append(Paragraph(item[1], st_p))
        else:
            _, header, rows = item
            data = [header] + rows
            widths = [170 * mm / len(header)] * len(header)
            table = Table(data, colWidths=widths, repeatRows=1)
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


# ---------------------------------------------------------------- DOCX 渲染（多级标题结构）
def render_docx(doc: Doc, path: Path):
    """带 Heading 多级结构、页脚页码域、表格的 Word 文档（python-docx）。"""
    import docx
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor

    d = docx.Document()
    normal = d.styles["Normal"]
    normal.font.name, normal.font.size = "Calibri", Pt(10.5)
    normal._element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), "宋体")
    for s, sz in (("Heading 1", 14), ("Heading 2", 12), ("Heading 3", 11)):
        st = d.styles[s]
        st.font.name, st.font.size, st.font.bold = "Calibri", Pt(sz), True
        st.font.color.rgb = RGBColor(0x1F, 0x3B, 0x57)
        st._element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), "黑体")

    def para(text, size=10.5, bold=False, align=None, font="宋体", indent=True, style=None):
        p = d.add_paragraph(style=style)
        if align is not None:
            p.alignment = align
        run = p.add_run(text)
        run.font.size, run.font.bold, run.font.name = Pt(size), bold, font
        run._element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), font)
        if indent and align is None and style is None:
            p.paragraph_format.first_line_indent = Pt(21)
        return p

    para(doc.title, 16, True, WD_ALIGN_PARAGRAPH.CENTER, "黑体")
    for line in doc.meta:
        para(line, 9, False, WD_ALIGN_PARAGRAPH.CENTER, "楷体", indent=False)
    for item in doc.items:
        if item[0] == "h":
            text = item[1]
            level = 1 if text.startswith(("第", "一", "二", "三", "四", "五", "六", "七", "八", "九", "十", "（一）", "（二）", "（三）")) and not text.startswith("（") else 2
            para(text, {1: 14, 2: 12}[level], True, None, "黑体", indent=False,
                 style="Heading 1" if level == 1 else "Heading 2")
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
    # 页脚页码域：第 X 页
    footer = d.sections[0].footer
    fp = footer.paragraphs[0]
    fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = fp.add_run("第 ")
    fld = OxmlElement("w:fldSimple")
    fld.set(qn("w:instr"), "PAGE \\* arabic")
    inner = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.text = "1"
    inner.append(t)
    fld.append(inner)
    fp._p.append(fld)
    fp.add_run(" 页")
    d.save(str(path))


# ---------------------------------------------------------------- XLSX 多工作表
def sheet_rows_months(header_spec, row_gen, n):
    return [row_gen(i) for i in range(1, n + 1)]


def make_xlsx(path: Path, sheets):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    wb = Workbook()
    wb.remove(wb.active)
    head_fill = PatternFill("solid", fgColor="4472C4")
    head_font = Font(bold=True, color="FFFFFF", name="微软雅黑", size=10)
    body_font = Font(name="微软雅黑", size=10)
    thin = Side(style="thin", color="B0B0B0")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    def fill_sheet(ws, title, header, rows, fmts=(), totals=None):
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
        if totals:  # totals: {col_letter: "=SUM(range)"} 追加合计行
            r = 3 + len(rows)
            ws.cell(r, 1, "合计").font = Font(bold=True, name="微软雅黑", size=10)
            for col_letter, rng in totals.items():
                c = ws[f"{col_letter}{r}"]
                c.value = f"=SUM({rng})"
                c.font = Font(bold=True, name="微软雅黑", size=10)
                if col_letter in fmts and fmts.get(col_letter):
                    c.number_format = fmts.get(col_letter, "General")
        ws.freeze_panes = "A3"
        for j, name in enumerate(header, 1):
            width = max([len(str(name))] + [len(str(r[j - 1])) for r in rows]) if rows else len(str(name))
            ws.column_dimensions[chr(64 + j) if j <= 26 else "A"].width = min(max(width * 1.9 + 2, 9), 40)

    for sheet in sheets:
        ws = wb.create_sheet(sheet["name"])
        fill_sheet(ws, sheet.get("title"), sheet["header"], sheet["rows"],
                   sheet.get("fmts", ()), sheet.get("totals"))
    wb.save(str(path))


def xlsx_specs():
    specs = []
    n = 55
    # 1 销售台账：明细 + 月度汇总 + 客户维度
    months = [f"2026-{m:02d}" for m in range(1, 13)]
    detail = [[f"SO{RNG.randrange(100000, 999999)}", company(), RNG.choice(["智能门禁系统", "工控主机", "网络交换机", "运维服务包"]),
               RNG.randrange(1, 40), RNG.randrange(200, 30000), None, RNG.choice(months)] for _ in range(n)]
    for r in detail:
        r[5] = r[3] * r[4]
    summary = [[m, str(RNG.randrange(30, 90)), str(RNG.randrange(60, 350)) + "万元"] for m in months]
    cust = [[company(), str(RNG.randrange(5, 40)), str(RNG.randrange(20, 300)) + "万元", RNG.choice(["合作中", "待续约"])]
            for _ in range(12)]
    specs.append((Path("36_销售台账.xlsx"), [
        {"name": "销售明细", "title": "2026年度销售明细台账", "header": ["订单号", "客户名称", "商品名称", "数量", "单价（元）", "金额（元）", "下单月份"],
         "rows": detail, "fmts": (None, None, None, "0", "#,##0", "#,##0", None), "totals": {"F": f"F3:F{2 + n}"}},
        {"name": "月度汇总", "title": "月度订单与金额汇总", "header": ["月份", "订单数", "销售金额"], "rows": summary},
        {"name": "客户维度", "title": "客户维度统计", "header": ["客户名称", "订单数", "累计金额", "状态"], "rows": cust},
    ]))
    # 2 部门预算：科目明细 + 分部门汇总 + 项目预算
    subjects = [["人员费用", RNG.randrange(50, 300)], ["办公费用", RNG.randrange(10, 80)],
                ["差旅费用", RNG.randrange(10, 60)], ["培训费用", RNG.randrange(5, 40)],
                ["系统运维", RNG.randrange(20, 120)], ["市场推广", RNG.randrange(15, 150)],
                ["咨询服务", RNG.randrange(10, 90)], ["其他费用", RNG.randrange(5, 30)]]
    specs.append((Path("37_部门年度预算表.xlsx"), [
        {"name": "预算科目", "title": "2027年度预算科目明细", "header": ["科目", "预算金额（万元）"],
         "rows": [[s, v] for s, v in subjects], "totals": {"B": f"B3:B{2 + len(subjects)}"}},
        {"name": "部门汇总", "title": "分部门预算汇总", "header": ["部门", "年度预算（万元）", "占比"],
         "rows": [[d, RNG.randrange(30, 260), f"{RNG.randrange(4, 18)}%"] for d in DEPTS]},
        {"name": "项目预算", "title": "重点项目预算", "header": ["项目", "预算（万元）", "责任部门"],
         "rows": [[p, RNG.randrange(20, 200), RNG.choice(DEPTS)] for p in PROJECTS]},
    ]))
    # 3 员工花名册：名册 + 部门统计 + 岗位分布
    roster = [[f"EMP{i:04d}", person(), RNG.choice(DEPTS), RNG.choice(POSTS),
               f"{RNG.randrange(2016, 2026)}-{RNG.randrange(1, 13):02d}-{RNG.randrange(1, 29):02d}",
               RNG.choice(["本科", "本科", "硕士", "大专"]), f"138{RNG.randrange(10000000, 99999999)}"]
              for i in range(1, 61)]
    dept_stat = [[d, str(RNG.randrange(15, 120)), str(RNG.randrange(12, 100))] for d in DEPTS]
    post_stat = [[p, str(RNG.randrange(8, 60)), f"{RNG.randrange(4, 20)}%"] for p in POSTS]
    specs.append((Path("38_员工花名册.xlsx"), [
        {"name": "花名册", "title": "员工花名册（60人）", "header": ["工号", "姓名", "部门", "岗位", "入职日期", "学历", "联系电话"], "rows": roster},
        {"name": "部门统计", "title": "部门人员统计", "header": ["部门", "编制数", "在岗数"], "rows": dept_stat},
        {"name": "岗位分布", "title": "岗位分布统计", "header": ["岗位", "人数", "占比"], "rows": post_stat},
    ]))
    # 4 项目进度：任务清单 + 里程碑 + 风险
    tasks = [[f"T{i:03d}", f"{RNG.choice(['需求分析', '系统设计', '开发实现', '联调测试', '上线部署', '培训推广'])}任务",
              person(), f"2026-{RNG.randrange(1, 12):02d}-{RNG.randrange(1, 29):02d}",
              RNG.randrange(5, 100), RNG.choice(["进行中", "已完成", "已完成", "未开始"])]
             for i in range(1, 41)]
    specs.append((Path("39_项目进度跟踪表.xlsx"), [
        {"name": "任务清单", "title": "项目任务清单（40项）", "header": ["任务号", "任务名称", "负责人", "计划日期", "进度（%）", "状态"], "rows": tasks},
        {"name": "里程碑", "title": "一级里程碑", "header": ["里程碑", "计划日期", "实际日期", "状态"],
         "rows": [["机房改造完成", "3月20日", "3月18日", "提前"], ["八大系统上线", "8月31日", "8月29日", "提前"],
                  ["数据中台上线", "11月15日", "11月12日", "提前"], ["智能应用上线", "11月30日", "11月20日", "提前"]]},
        {"name": "风险清单", "title": "项目风险登记", "header": ["风险描述", "等级", "应对措施"],
         "rows": [["历史数据质量不高", "高", "专项清洗与源头治理"], ["接口改造延期", "中", "提前联调与专人跟进"],
                  ["人员流失", "中", "关键岗位AB角与知识沉淀"]]},
    ]))
    # 5 库存（多仓库） + 6 考勤（多月） + 7 固定资产 + 8 订单汇总 + 9 应收账龄 + 10 供应商
    inv1 = [[f"M{RNG.randrange(10000, 99999)}", RNG.choice(["门禁控制器", "读卡器", "电控锁", "网线", "服务器内存条"]),
             RNG.randrange(0, 300), RNG.randrange(10, 80)] for _ in range(30)]
    inv2 = [[f"M{RNG.randrange(10000, 99999)}", RNG.choice(["打印机", "考勤机", "投影仪", "消防应急灯"]),
             RNG.randrange(0, 200), RNG.randrange(5, 50)] for _ in range(24)]
    specs.append((Path("40_多仓库库存清单.xlsx"), [
        {"name": "主仓库", "title": "主仓库存货清单", "header": ["物料编码", "物料名称", "库存数量", "安全库存"], "rows": inv1},
        {"name": "分仓库", "title": "分仓库存货清单", "header": ["物料编码", "物料名称", "库存数量", "安全库存"], "rows": inv2},
    ]))
    att = [[f"EMP{RNG.randrange(1, 60):04d}", person(), m, RNG.randrange(18, 23), RNG.randrange(0, 4), RNG.randrange(0, 20)]
           for m in ["2026-08", "2026-09", "2026-10"] for _ in range(12)]
    specs.append((Path("41_季度考勤统计表.xlsx"), [
        {"name": "考勤明细", "title": "季度考勤统计（3个月）", "header": ["工号", "姓名", "月份", "实出勤（天）", "迟到（次）", "加班（小时）"], "rows": att},
        {"name": "加班排行", "title": "加班时长排行", "header": ["姓名", "部门", "季度加班（小时）"],
         "rows": [[person(), RNG.choice(DEPTS), RNG.randrange(10, 80)] for _ in range(15)]},
    ]))
    assets = [[f"FA{RNG.randrange(10000, 99999)}", RNG.choice(["台式电脑", "笔记本", "服务器", "打印机", "考勤机"]),
               RNG.choice(DEPTS), RNG.randrange(2000, 90000), RNG.choice(["在用", "在用", "闲置", "维修中"])]
              for _ in range(35)]
    specs.append((Path("42_固定资产台账.xlsx"), [
        {"name": "资产台账", "title": "固定资产台账（35项）", "header": ["资产编号", "设备名称", "使用部门", "原值（元）", "状态"],
         "rows": assets, "fmts": (None, None, None, "#,##0", None), "totals": {"D": f"D3:D{2 + 35}"}},
        {"name": "分类汇总", "title": "设备分类汇总", "header": ["类别", "数量", "原值合计（元）"],
         "rows": [[k, str(RNG.randrange(5, 20)), str(RNG.randrange(50000, 600000))] for k in ["电脑类", "服务器类", "外设类", "其他"]]},
    ]))
    orders = [[f"PO{RNG.randrange(100000, 999999)}", company(), RNG.choice(GOODS)[0],
               RNG.randrange(3000, 300000), f"2026-{RNG.randrange(1, 13):02d}-{RNG.randrange(1, 29):02d}",
               RNG.choice(["已到货", "在途", "部分到货"])] for _ in range(45)]
    specs.append((Path("43_采购订单汇总表.xlsx"), [
        {"name": "订单明细", "title": "年度采购订单（45单）", "header": ["订单号", "供应商", "货物名称", "订单金额（元）", "下单日期", "到货状态"],
         "rows": orders, "fmts": (None, None, None, "#,##0", None, None), "totals": {"D": f"D3:D{2 + 45}"}},
        {"name": "供应商统计", "title": "供应商交易统计", "header": ["供应商", "订单数", "交易金额（万元）"],
         "rows": [[company(), str(RNG.randrange(2, 15)), str(RNG.randrange(10, 200))] for _ in range(10)]},
    ]))
    ar = [[company(), round(RNG.uniform(5, 300), 1), round(RNG.uniform(0, 120), 1), round(RNG.uniform(0, 80), 1),
           round(RNG.uniform(0, 40), 1), RNG.choice(["正常", "重点跟进", "已发函催收"])] for _ in range(20)]
    specs.append((Path("44_应收账款账龄分析表.xlsx"), [
        {"name": "账龄明细", "title": "应收账款账龄明细（20户）", "header": ["客户名称", "应收合计（万元）", "1个月内", "1-3个月", "3-6个月", "状态"], "rows": ar},
        {"name": "账龄汇总", "title": "账龄区间汇总", "header": ["账龄区间", "金额（万元）", "占比"],
         "rows": [["1个月内", str(RNG.randrange(100, 400)), "42%"], ["1-3个月", str(RNG.randrange(50, 200)), "23%"],
                  ["3-6个月", str(RNG.randrange(20, 100)), "15%"], ["6个月以上", str(RNG.randrange(10, 60)), "20%"]]},
    ]))
    suppliers = [[company(), RNG.choice(["A", "B", "C"]), person(), f"139{RNG.randrange(10000000, 99999999)}",
                  RNG.choice(["合作中", "考察期", "暂停"]), str(RNG.randrange(1, 8)) + "年"] for _ in range(18)]
    specs.append((Path("45_供应商名录与评级.xlsx"), [
        {"name": "供应商名录", "title": "合格供应商名录（18家）", "header": ["供应商名称", "评级", "联系人", "联系电话", "合作状态", "合作年限"], "rows": suppliers},
        {"name": "评级说明", "title": "供应商评级标准", "header": ["评级", "评分区间", "政策"],
         "rows": [["A", "90分及以上", "优先合作，扩大份额"], ["B", "75至89分", "正常合作，年度复评"], ["C", "60至74分", "限制份额，限期整改"]]},
    ]))
    return specs


# ---------------------------------------------------------------- PPTX 12+ 页
def make_pptx(path, title, sections, table_slide=None):
    from pptx import Presentation
    from pptx.util import Inches, Pt

    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)

    def add(layout_idx, title_text, bullets, bold_first=False):
        slide = prs.slides.add_slide(prs.slide_layouts[layout_idx])
        slide.shapes.title.text = title_text
        slide.shapes.title.text_frame.paragraphs[0].runs[0].font.size = Pt(30)
        if bullets:
            body = slide.placeholders[1].text_frame
            body.word_wrap = True
            for i, b in enumerate(bullets):
                p = body.paragraphs[0] if i == 0 else body.add_paragraph()
                run = p.add_run()
                run.text = b
                run.font.size = Pt(18)
                run.font.name = "微软雅黑"
        return slide

    add(0, title, [f"汇报人：{person()}  |  {company()}", f"{cn_date(rand_date())}"])
    add(1, "目录", [s[0] for s in sections] + ["总结与下一步"])
    for sec_title, slides in sections:
        for i, bullets in enumerate(slides):
            add(1, sec_title if i == 0 else f"{sec_title}（续{i}）", bullets)
    if table_slide:
        slide = prs.slides.add_slide(prs.slide_layouts[5])
        slide.shapes.title.text = table_slide[0]
        slide.shapes.title.text_frame.paragraphs[0].runs[0].font.size = Pt(28)
        rows, cols = len(table_slide[1]) + 1, len(table_slide[1][0])
        tbl = slide.shapes.add_table(rows, cols, Inches(1), Inches(1.8), Inches(11), Inches(0.4 * rows)).table
        for j, h in enumerate(table_slide[1][0]):
            tbl.cell(0, j).text = h
        for i, row in enumerate(table_slide[1][1:], 1):
            for j, v in enumerate(row):
                tbl.cell(i, j).text = str(v)
    add(1, "总结与下一步", ["年度目标全面达成，三项超额完成", "数据治理与流程贯通是下阶段重点",
                       "持续投入智能化应用，扩大赋能范围", "感谢聆听，欢迎交流指正"])
    prs.save(str(path))


def pptx_specs():
    bullets_bank = [
        ["营业收入完成年度目标的{n}%，符合序时进度", "主营业务毛利率稳定在{m}%左右", "期间费用率{s}%，同比基本持平"],
        ["智慧园区项目新签合同额同比增长{n}%", "大客户续约率达到{m}%", "交付准时率提升至{s}%"],
        ["部分项目回款周期偏长，长账龄客户增多", "新区域市场拓展进度低于预期", "核心岗位招聘存在缺口，交付资源紧张"],
        ["推进应收账款专项清收，压降逾期余额", "启动区域渠道合作，扩大市场覆盖", "优化组织与编制，补齐关键岗位"],
    ]
    specs = []
    for i, (fname, title, sec_titles) in enumerate([
        ("46_年度经营分析汇报.pptx", "年度经营分析汇报", ["经营概览", "业务亮点", "问题与不足", "下季度计划"]),
        ("47_数字化转型项目总结汇报.pptx", "数字化转型项目总结汇报", ["项目概况", "建设成果", "运行数据", "经验与计划"]),
        ("48_新员工入职培训.pptx", "新员工入职培训", ["公司简介", "制度速览", "信息安全", "成长路径"]),
        ("49_安全生产年度宣导.pptx", "安全生产年度宣导", ["安全形势", "常见隐患", "应对措施", "应急处置"]),
        ("50_市场拓展策略汇报.pptx", "市场拓展策略汇报", ["市场回顾", "竞争格局", "策略举措", "目标分解"]),
    ]):
        fills = lambda: dict(n=RNG.randrange(90, 120), m=RNG.randrange(20, 40), s=RNG.randrange(10, 25))
        sections = [(t, [[b.format(**fills()) for b in bullets_bank[k % 4]],
                    [b.format(**fills()) for b in bullets_bank[(k + 1) % 4]]]) for k, t in enumerate(sec_titles)]
        table_slide = (f"{sec_titles[0]}关键数据",
                       [["指标", "本期", "上期", "增减"],
                        ["营业收入（万元）", RNG.randrange(500, 3000), RNG.randrange(400, 2800), f"+{RNG.randrange(3, 25)}%"],
                        ["新签客户（家）", RNG.randrange(10, 60), RNG.randrange(8, 50), f"+{RNG.randrange(4, 30)}%"],
                        ["人均产出（万元）", RNG.randrange(30, 90), RNG.randrange(28, 80), f"+{RNG.randrange(2, 15)}%"]])
        specs.append((Path(fname), title, sections, table_slide))
    return specs


# ---------------------------------------------------------------- 组装 50 份
def manual_doc():
    doc = Doc("星河智联V3门禁平台部署与运维手册", [company(), f"文档版本：V3.{RNG.randrange(1, 9)}", f"发布日期：{cn_date(rand_date())}", "密级：内部"])
    chapters = [
        ("一、产品概述", ["星河智联V3门禁平台是面向园区与楼宇的一体化通行管理系统，支持人脸、卡片、二维码多种通行介质，提供设备管理、权限管理、通行记录与访客管理四大功能模块。",
                   "平台采用云端订阅与本地部署双模式，本地部署最小规模支持三台控制器、五百个账号，可平滑扩展至园区级万点规模。"]),
        ("二、部署前准备", ["服务器配置不低于八核CPU、十六GB内存、五百GB硬盘；操作系统支持主流Linux发行版；浏览器推荐使用最新版Chrome或Edge。",
                     "网络环境需保证门禁设备与管理平台之间双向可达，设备侧需预留固定IP地址段；跨网段部署时应提前开通防火墙策略。"]),
        ("三、安装步骤", ["执行安装包并按向导完成数据库初始化；首次登录使用默认管理员账号并立即修改密码。",
                    "在【设备管理】中添加门禁控制器并配置所在区域；在【权限管理】中建立人员组织树并下发通行权限；在【系统设置】中配置备份策略与告警通知方式。",
                    "安装完成后应执行连通性自检，确认全部设备在线且通行记录可正常回传。"]),
        ("四、日常运维", ["每日检查平台运行状态与设备在线率，每周核查通行记录完整性，每月执行一次数据备份与恢复演练。",
                    "账号权限实行最小化原则，人员异动应在三个工作日内完成权限调整；平台账号不得共享。"]),
        ("五、常见故障处理", ["设备离线：优先检查网络与供电，其次检查设备心跳配置。",
                      "识别失败率升高：重新采集人脸照片并保证光照充足，必要时调整摄像头角度。",
                      "数据异常：使用【数据修复】工具自检，无法恢复时联系技术支持并提交日志包。",
                      "权限下发失败：检查人员组织树节点与设备分组映射，重新执行全量下发。"]),
        ("六、备份与恢复", ["平台数据库每日凌晨自动全量备份，备份文件保留三十天；每月制作一份异地备份并保留一年。",
                     "恢复演练每半年组织一次，验证备份可用性与恢复时长；恢复目标为四小时内恢复业务。"]),
        ("七、技术支持", ["技术支持热线工作时间为工作日9:00至18:00，紧急故障提供7×24小时支持。",
                     "提交工单时请附设备编号、故障时间与现场照片，以便快速定位。"]),
    ]
    for h, paras in chapters:
        doc.h(h)
        for p in paras:
            doc.p(p)
    doc.sign = [company(), "本手册内容如有更新，以最新发布版本为准"]
    return doc


def minutes_doc():
    when = rand_date()
    doc = Doc("项目联席会议纪要", [f"会议时间：{cn_date(when)} 14:30—17:00", "会议地点：公司3楼1号会议室",
                                f"主持人：{person()}", f"记录人：{person()}", f"纪要编号：{doc_no('HY')}"])
    attendees = [person() for _ in range(8)]
    doc.p("出席人员：" + "、".join(attendees) + "。列席人员：" + person() + "（财务部）。")
    doc.h("一、会议议题")
    doc.p(f"议题一：听取{RNG.choice(PROJECTS)}项目进展汇报；议题二：审议下阶段资源调整方案；议题三：通报年度考核结果并讨论改进事项。")
    doc.h("二、议题讨论情况")
    doc.p("（一）项目组汇报了总体进度、已完成的里程碑和当前存在的主要问题。与会人员围绕进度偏差、人力缺口与外部依赖三个专题进行了充分讨论，形成如下共识：项目总体可控，但需重点解决跨部门协同与测试环境不足两个瓶颈。")
    doc.p("（二）关于资源调整，人力资源部通报了当前编制与招聘进展，建议通过内部调剂与外部补充相结合的方式解决交付资源缺口。财务部就预算调整的审批流程作了说明。")
    doc.p("（三）年度考核结果通报后，各部门负责人分别就本部门失分项作了说明，并提出改进思路。")
    doc.h("三、会议决议")
    for i in range(1, 5):
        doc.p(f"（{ '一二三四'[i-1] }）由{RNG.choice(DEPTS)}牵头、{RNG.choice(DEPTS)}配合，两周内完成第{i}项决议事项的落实，并将书面反馈报送综合管理部；逾期未反馈的，纳入部门月度考核。")
    doc.h("四、后续安排")
    doc.p("下次联席会议定于一个月后召开，各责任部门应于会前三日提交进展材料。")
    doc.sign = [company(), f"{cn_date(when)}", "抄送：各部门负责人"]
    return doc


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    counts = {"pdf": 0, "docx": 0, "xlsx": 0, "pptx": 0}
    pdf_specs, docx_specs_list = [], []

    kinds = [c[0] for c in CONTRACT_TYPES]
    # PDF 20：7 长合同 + 8 长报告 + 制度/纪要/手册/验收等 5
    for i, kind in enumerate(kinds):
        extra = CONTRACT_TYPES[i][1]
        annex_a = ("货物清单", ["货物名称", "单位", "数量", "单价（元）"], goods_rows(5)) if kind == "采购合同" else (
            "付款计划表", ["期次", "支付条件", "比例", "金额（元）"],
            [["第一期", "合同生效后", "30%", str(RNG.randrange(100000, 900000))],
             ["第二期", "到货初验合格", "30%", str(RNG.randrange(100000, 900000))],
             ["第三期", "终验合格", "35%", str(RNG.randrange(100000, 900000))],
             ["质保金", "质保期满", "5%", str(RNG.randrange(10000, 90000))]])
        annex_b = ("技术与服务要求", ["序号", "要求项", "说明"],
                   [[str(j), req, desc] for j, (req, desc) in enumerate([
                       ("响应时间", "故障响应不超过30分钟"), ("服务窗口", "7×24小时热线支持"),
                       ("驻场服务", "重大变更提供驻场保障"), ("培训", "每年不少于两场")], 1)])
        annex_c = ("考核指标表", ["考核项", "指标", "权重", "扣分规则"],
                   [["交付进度", "按里程碑按期交付", "30%", "每延期一日扣2分"],
                    ["交付质量", "验收一次通过", "30%", "复验一次扣10分"],
                    ["服务响应", "4小时内响应", "20%", "超时一次扣3分"],
                    ["配合程度", "资料与人员及时到位", "20%", "不到位一次扣5分"]])
        pdf_specs.append((f"{i + 1:02d}_{kind}.pdf", contract_doc(kind, extra, annex_a, annex_b, annex_c)))
    report_defs = [
        ("项目验收报告", "受建设单位委托，验收组对项目合同约定的全部建设内容组织验收，本次验收范围为功能实现、性能指标与文档交付三部分。", ["验收范围与方法", "功能验收情况", "性能与安全测试"]),
        ("年度财务分析报告", "本报告依据公司年度财务报表及经营数据编制，对收入、成本、费用、现金流及主要财务指标进行分析，为经营决策提供参考。", ["收入与利润分析", "成本费用分析", "现金流与偿债能力"]),
        ("可行性研究报告", "为满足业务发展需要，公司拟实施新平台建设项目，本报告从业务需求、技术方案、投资估算与效益预测等方面论证项目可行性。", ["需求分析", "技术方案比选", "投资估算与效益预测"]),
        ("年度审计报告", "我们接受委托，对公司年度财务报表进行了审计，审计范围包括资产负债表、利润表、现金流量表及财务报表附注。", ["审计范围与程序", "审计发现", "内部控制评价"]),
        ("风险评估报告", "本报告对公司面临的市场、运营、财务与合规四类主要风险进行识别与评估，评估采用定性分析与关键指标量化相结合的方法。", ["市场风险评估", "运营风险评估", "财务与合规风险"]),
        ("市场调研报告", "本次调研覆盖华东、华南两个重点区域，采取问卷调查与客户访谈相结合的方式，共回收有效问卷412份，深度访谈客户16家。", ["调研设计", "需求特征分析", "竞争格局与机会"]),
        ("运营分析报告", "本报告对全年运营数据进行系统分析，覆盖订单履约、库存周转、客户服务与成本控制四个维度，定位薄弱环节并提出改进建议。", ["履约与库存分析", "客户服务分析", "成本控制分析"]),
        ("尽职调查报告", "受委托方委托，调查组对目标公司的主体资格、资产、业务、财务与法律状况开展了尽职调查，调查方式包括资料审阅、现场走访与访谈。", ["主体与股权结构", "业务与财务状况", "法律风险核查"]),
    ]
    for j, (title, intro, chaps) in enumerate(report_defs):
        pdf_specs.append((f"{len(kinds) + j + 1:02d}_{title}.pdf",
                          report_doc(title, intro, chaps, tables=report_tables())))
    pdf_specs.append(("16_员工差旅费管理办法.pdf", rules_doc("员工差旅费管理办法", [
        ("交通标准", "员工出差乘坐交通工具的标准为：高层管理人员可乘坐飞机公务舱及高铁一等座；中层管理人员可乘坐飞机经济舱及高铁一等座；其他人员可乘坐高铁二等座。"),
        ("住宿标准", "住宿费实行限额内凭票报销，一类城市每人每晚不超过五百元，其他城市不超过三百五元。"),
        ("报销流程", "差旅费应在费用发生后一个月内报销，报销时须提供行程审批单、票据及出差报告，经部门负责人与财务部审核后支付。"),
        ("特殊情况", "因特殊情况超标准乘坐交通工具或住宿的，应事先书面报分管领导批准，否则超支部分自理。")])))
    pdf_specs.append(("17_项目联席会议纪要.pdf", minutes_doc()))
    pdf_specs.append(("18_产品部署与运维手册.pdf", manual_doc()))
    pdf_specs.append(("19_采购询价函.pdf", Doc("采购询价函", None)))
    pdf_specs.append(("20_年度安全生产工作总结.pdf", report_doc(
        "年度安全生产工作总结", "本年度公司坚持安全第一、预防为主、综合治理的方针，落实安全生产责任制，全年未发生重大安全事故，安全生产形势总体平稳。",
        ["责任体系与制度建设", "隐患排查与治理", "培训与应急演练"], tables=report_tables())))

    # DOCX 15：制度/方案/总结类（多级标题结构）
    docx_defs = [
        ("21_员工手册（试行）.docx", rules_doc("员工手册（试行）", [
            ("聘用与入职", "公司实行劳动合同制，新员工入职应提交学历证明、体检报告等材料，试用期按劳动合同约定执行，试用期考核合格后办理转正。"),
            ("考勤管理", "公司实行标准工时制度，员工应按排班准时到岗；迟到、早退按次累计统计，月度累计三次以上的，按制度予以处理。"),
            ("薪酬与福利", "薪酬由基本工资、岗位工资、绩效工资与津补贴构成，于每月十五日发放；公司依法为员工缴纳社会保险与住房公积金。"),
            ("奖惩制度", "对工作业绩突出、合理化建议被采纳或有其他显著贡献的员工予以奖励；违反规章制度的，视情节轻重给予相应处分。")])),
        ("22_采购管理办法.docx", rules_doc("采购管理办法", [
            ("采购方式", "采购分为公开招标、邀请招标、竞争性谈判与单一来源四种方式，达到限额标准的采购项目原则上采用公开招标方式。"),
            ("供应商管理", "建立合格供应商名录，实行准入评估与年度复评；供应商存在重大违约或质量问题的，移出名录并在两年内不得重新申请。"),
            ("合同与验收", "采购合同应明确标的、价格、交付、验收与违约责任；到货后由使用部门与采购部门联合验收，验收记录作为付款依据。")])),
        ("23_信息安全管理制度.docx", rules_doc("信息安全管理制度", [
            ("账号与权限", "信息系统账号实行实名制，按最小权限原则授权；人员岗位变动时应同步调整权限，离职当日注销全部账号。"),
            ("数据安全", "重要数据实行分类分级管理，涉密数据的存储、传输与销毁应符合公司数据安全规范；外发数据须经部门负责人审批。"),
            ("终端安全", "办公终端应安装公司统一的终端安全软件，及时更新补丁；不得安装未经授权的软件，不得擅自外接存储设备。")])),
        ("24_固定资产管理办法.docx", rules_doc("固定资产管理办法", [
            ("资产分类", "固定资产按用途分为房屋建筑物、机器设备、电子设备、运输工具四类，单位价值两千元以上且使用年限超过一年的纳入固定资产管理。"),
            ("日常管理", "资产购置后一个月内完成验收与登记，粘贴资产标签；每年组织一次全面盘点，盘盈盘亏应查明原因并按规定审批处理。"),
            ("处置管理", "资产报废、转让应由使用部门提出申请，经技术鉴定与逐级审批后处置，处置收入及时入账。")])),
        ("25_关于开展年度资产盘点工作的通知.docx", Doc("关于开展年度资产盘点工作的通知", [f"发文编号：{doc_no()}", f"发文日期：{cn_date(rand_date())}"])),
        ("26_关于组织消防安全培训的通知.docx", Doc("关于组织消防安全培训的通知", [f"发文编号：{doc_no()}", f"发文日期：{cn_date(rand_date())}"])),
        ("27_信息系统上线切换公告.docx", Doc("关于新版办公OA系统上线切换的公告", [f"公告编号：{doc_no('GG')}", f"发布日期：{cn_date(rand_date())}"])),
        ("28_部门年度工作总结.docx", report_doc(f"{RNG.choice(DEPTS)}年度工作总结",
            "本年度部门围绕公司重点任务，扎实推进各项工作，在项目交付、流程优化与团队建设方面取得阶段性成效。现将主要情况总结如下。",
            ["重点工作完成情况", "团队建设情况", "存在的问题"], tables=report_tables())),
        ("29_客户投诉处理报告.docx", report_doc("客户投诉处理报告",
            f"客服中心收到客户关于设备偶发失灵的投诉（工单号TS{RNG.randrange(1000, 9999)}），客服中心会同相关部门进行调查处理，现将情况报告如下。",
            ["事件经过", "原因分析", "处理措施"], tables=report_tables())),
        ("30_供应商准入评估报告.docx", report_doc("供应商准入评估报告",
            f"根据采购管理制度，采购部组织对候选供应商开展准入评估，评估组由采购、质量、财务三部门人员组成，通过资料审查、现场考察与样品测试三种方式实施。",
            ["评估过程", "评估结果", "准入建议"], tables=report_tables())),
        ("31_设备年度维护保养计划.docx", report_doc("生产设备年度维护保养计划",
            "为保障设备安全稳定运行，降低故障停机风险，依据设备管理制度与制造商维保建议，编制本年度维护保养计划。",
            ["维保范围与分工", "保养内容与周期", "保障措施"], tables=report_tables())),
        ("32_加班与调休管理办法.docx", rules_doc("加班与调休管理办法", [
            ("加班审批", "加班须事先通过办公系统申请并经部门负责人批准，未经审批的加班不予认定。"),
            ("调休规则", "工作日加班可按1:1申请调休，休息日加班按1:1.5、法定节假日加班按1:2折算调休，调休原则上在三个月内使用完毕。")])),
        ("33_内部审计整改通知书.docx", Doc("关于内部审计发现问题整改的通知", [f"发文编号：{doc_no('SJ')}", f"发文日期：{cn_date(rand_date())}"])),
        ("34_合作备忘录.docx", minutes_doc()),
        ("35_市场推广活动策划方案.docx", report_doc("市场推广活动策划方案",
            "为扩大品牌影响力并支撑年度销售目标，市场部拟于下一季度组织系列推广活动，本方案就活动主题、安排、预算与效果评估进行策划。",
            ["活动主题与对象", "活动安排", "预算与效果评估"], tables=report_tables())),
    ]
    notice_bodies = {
        "25_关于开展年度资产盘点工作的通知.docx": [
            "公司各部门：为摸清公司资产家底，加强实物资产管理，经研究决定于12月开展年度资产盘点工作。现将有关事项通知如下。",
            "一、盘点范围：公司全部固定资产与低值易耗品，盘点基准日为11月30日。",
            "二、组织方式：由财务部牵头，各部门指定专人配合；12月5日前完成自查并报送资产自查表，12月10日起盘点小组现场复核。",
            "三、结果处理：盘点中发现盘盈、盘亏或毁损的，应查明原因、明确责任，并按公司资产管理制度办理审批手续。",
            "四、工作要求：各部门要高度重视盘点工作，如实填报数据，不得瞒报漏报；盘点期间原则上暂停资产调拨，确需调拨的应报盘点小组备案。",
            "五、联系方式：盘点工作联系人及联系电话以工作群通知为准。特此通知。"],
        "26_关于组织消防安全培训的通知.docx": [
            "公司各部门：为进一步增强员工消防安全意识，提高火灾防范与应急处置能力，公司决定组织开展消防安全专题培训。现将有关事项通知如下。",
            "一、培训时间与地点：12月9日（周三）下午14:00，公司一楼多功能厅。",
            "二、培训对象：全体员工；各部门可根据排班分两批参加。",
            "三、培训内容：消防法律法规解读、典型火灾案例分析、灭火器材实操演练与疏散逃生要点。",
            "四、相关要求：请各部门合理安排工作，组织员工准时参加，参训情况纳入部门安全考核。",
            "五、其他事项：培训现场将组织灭火器实操体验，请穿着便于活动的服装；培训课件会后共享至部门资料库。特此通知。"],
        "27_信息系统上线切换公告.docx": [
            "全体员工：为提升办公效率，公司已完成新版办公OA系统的建设与测试，定于近期正式上线切换。现将有关事项公告如下。",
            "一、切换安排：旧系统将于切换日18:00停止受理新流程，未办结事项由各部门导出留档，次日起全部业务在新系统办理。",
            "二、账号与密码：新系统账号沿用工号，初始密码将通过企业微信下发，首次登录后须修改密码并完成手机号绑定。",
            "三、使用支持：系统使用手册已发布至共享盘，如有问题请联系数字化办公室，服务分机8001。",
            "四、注意事项：切换期间各流程发起人应关注待办事项提醒；跨期流程的衔接规则详见操作指引第八节。",
            "五、后续安排：系统上线满一个月后组织使用情况回访，集中收集改进意见。特此公告。"],
        "33_内部审计整改通知书.docx": [
            f"{RNG.choice(DEPTS)}：根据公司年度内部审计计划，审计组近期对你部门开展了专项审计，发现合同管理、费用报销等方面存在待完善事项。",
            "一、整改要求：请对照《审计发现问题清单》逐项分析原因，制定整改措施，明确责任人与完成时限。",
            "二、反馈时限：请于收到本通知后20个工作日内报送整改报告及佐证材料。",
            "三、后续安排：审计组将适时组织整改效果复查，复查未通过的整改事项将升级管理。",
            "四、附件：《审计发现问题清单》一份。特此通知。"],
    }
    for filename, doc_model in docx_defs:
        if filename in notice_bodies and not doc_model.items:
            doc_model.p(notice_bodies[filename][0])
            for para in notice_bodies[filename][1:-1]:
                doc_model.p(para)
            doc_model.p(notice_bodies[filename][-1])
            doc_model.sign = [company(), cn_date(rand_date())]
        pdf_or_docx = filename.endswith(".pdf")
        path = OUT / filename
        render_pdf(doc_model, path) if pdf_or_docx else render_docx(doc_model, path)
        counts["docx" if not pdf_or_docx else "pdf"] += 1
        print(filename)
    for filename, doc_model in pdf_specs:
        path = OUT / filename
        if filename == "19_采购询价函.pdf":
            doc_model.meta = [f"致：{company()}", f"询价日期：{cn_date(rand_date())}", "报价有效期：30日"]
            doc_model.p("我司拟采购以下设备：" + "、".join(g[0] for g in RNG.sample(GOODS, 4)) + "，具体数量与技术要求见附件。")
            doc_model.table(["序号", "设备名称", "数量", "技术要求"],
                            [[str(j), g[0], str(RNG.randrange(2, 60)), RNG.choice(["标准型", "增强型", "含安装调试"])]
                             for j, g in enumerate(RNG.sample(GOODS, 4), 1)])
            doc_model.p("请贵司于收函后5个工作日内书面回复报价（含税、含运费），并注明供货周期与质保期限。本次询价不构成采购承诺，我司将根据报价、资质与服务能力综合确定合作方。")
            doc_model.sign = [company(), f"联系人：{person()}", f"电话：0{RNG.randrange(10, 99)}-{RNG.randrange(1000000, 9999999)}"]
        render_pdf(doc_model, path)
        counts["pdf"] += 1
        print(filename)
    for path, sheets in xlsx_specs():
        make_xlsx(OUT / path, sheets)
        counts["xlsx"] += 1
        print(path.name)
    for path, title, sections, table_slide in pptx_specs():
        make_pptx(OUT / path, title, sections, table_slide)
        counts["pptx"] += 1
        print(path.name)
    total = sum(counts.values())
    print(f"\n共 {total} 份 → {OUT}")
    print("格式分布：", counts)
    assert total == 50, counts
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
