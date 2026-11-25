"""直接回答能从同一份制度核实的表格、计算和页码问题。"""
import re
from collections import defaultdict
from decimal import Decimal

PAGE_QUESTION = re.compile(r"第几页|哪一?页|页码|页脚")
REPORT_HEADING = "4.10设计回访"


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _ref(block) -> str:
    return f"[{block['n']}]"


def _number(value: Decimal) -> str:
    plain = format(value, "f")
    return plain.rstrip("0").rstrip(".") if "." in plain else plain


def _table(block):
    rows = block.get("policy_table")
    if rows is None and block.get("header"):
        rows = [block["header"], *block.get("rows", [])]
    return [[_compact(cell) for cell in row] for row in rows or [] if row]


def _change(question, evidence):
    voltage = re.search(r"(?<!\d)(\d+)kV", question, re.I)
    amount = re.search(r"(?<![\d.,])(\d+(?:\.\d+)?)万元", question)
    share = re.search(r"(?<![\d.,])(\d+(?:\.\d+)?)[%％]", question)
    if not (voltage and amount and share and "设计变更" in question and re.search(r"属于|哪一类|哪类", question)):
        return ""
    voltage, amount, share = voltage[1], Decimal(amount[1]), Decimal(share[1])
    for block in evidence:
        rows = _table(block)
        if not rows or "电压" not in rows[0][0]:
            continue
        column = next((i for i, name in enumerate(rows[0])
                       if re.search(rf"(?<!\d){voltage}kV", name, re.I)), None)
        if column is None:
            continue
        major = next((row[column] for row in rows if len(row) > column and row[0] == "重大设计变更"), "")
        rule = re.fullmatch(r"变更金额≥(\d+(?:\.\d+)?)万元或变更金额与单项工程概算比率≥(\d+(?:\.\d+)?)%", major)
        if not rule:
            continue
        money, ratio = Decimal(rule[1]), Decimal(rule[2])
        if amount < money and share < ratio:
            continue
        why = (f"金额 {_number(amount)} 万元已达到 {_number(money)} 万元门槛" if amount >= money
               else f"占概算比例 {_number(share)}% 已达到 {_number(ratio)}% 门槛")
        conclusion = f"属于**重大设计变更** {_ref(block)}。"
        if re.search(r"由谁|签署|签字|批准", question):
            for approval in evidence:
                if approval.get("source_id") != block.get("source_id"):
                    continue
                signer = re.search(r"重大设计变更由([^；。/、]{1,16})签署批准",
                                   _compact(approval.get("text", "")))
                if signer:
                    conclusion = f"属于**重大设计变更**，由**{signer[1]}**签署批准 {_ref(approval)}。"
                    break
            else:
                continue
        return (f"{conclusion}\n\n{voltage} kV 的重大变更条件是金额达到 {_number(money)} 万元，"
                f"或占概算比例达到 {_number(ratio)}%。本题{why} {_ref(block)}。")
    return ""


def _mileage(question, evidence):
    distance = re.search(r"(?<![\d.,])(\d+(?:\.\d+)?)公里", question)
    if not distance or "自驾" not in question or "出差" not in question or not re.search(r"报销|补助", question):
        return ""
    if (not re.search(r"经批准|获批准|已批准|已获批", question)
            or re.search(r"未经批准|未获批准|未批准|未获批|没有批准", question)):
        return ""
    for block in evidence:
        text = _compact(block.get("text", ""))
        rate = re.search(r"按每公里(\d+(?:\.\d+)?)元", text)
        if not rate or not all(term in text for term in ("高速通行费", "停车费", "凭票", "不再报销同程城市间交通费")):
            continue
        for other in evidence:
            if other.get("source_id") != block.get("source_id"):
                continue
            if not re.search(r"自驾[^。；]{0,20}不再发放市内交通费", _compact(other.get("text", ""))):
                continue
            amount = _number(Decimal(distance[1]) * Decimal(rate[1]))
            return (f"里程补助 **{amount} 元**（{distance[1]} 公里 × {rate[1]} 元/公里），"
                    f"高速通行费和停车费凭票报销 {_ref(block)}。\n\n"
                    f"不再报销同程城市间交通费 {_ref(block)}，也不发放市内交通费 {_ref(other)}。")
    return ""


def _trip_total(question, evidence):
    destination = re.search(r"(?:去|到|赴)([\u4e00-\u9fff]{2,10}?)(?:市)?出差", question)
    level = re.search(r"正高级|副高级|中级", question)
    days = re.search(r"(\d+)(?:个)?(?:自然日|天)", question)
    nights = re.search(r"住(\d+)晚", question)
    meals = re.search(r"安排(?:了)?(\d+)顿饭", question)
    if not all((destination, level, days, nights, meals, re.search(r"合计|总计", question),
                re.search(r"(?:没有|未)提供交通工具", question), "自驾" not in question, "上限" in question)):
        return ""
    city = destination[1]
    title = "中级及以下" if level[0] == "中级" else level[0]
    by_source = defaultdict(list)
    for block in evidence:
        if block.get("source_id") is not None:
            by_source[block["source_id"]].append(block)
    totals = []
    for source in by_source.values():
        city_block = grade = meal_block = transit_block = lodging_block = None
        food_rate = reduction = transit_rate = room_rate = None
        for block in source:
            text = _compact(block.get("text", ""))
            lists = re.findall(r"([一二三]类城市)[：:]([^。；;]+)", text)
            for category, names in lists:
                if any(city == name.removesuffix("市") for name in re.split(r"[、,，]", names)):
                    city_block, grade = block, category
            if "伙食补助" in text:
                daily = re.search(r"标准为每人每天(\d+(?:\.\d+)?)元", text)
                minus = re.search(r"每餐(\d+(?:\.\d+)?)元", text)
                if daily and minus:
                    meal_block, food_rate, reduction = block, Decimal(daily[1]), Decimal(minus[1])
            if re.search(r"^【第[一二三四五六七八九十百\d]+条续】\d+(?:\.\d+)?元。[^。]*提供交通工具[^。]*市内交通费", text):
                transit_block = block
                transit_rate = Decimal(re.search(r"】([\d.]+)元", text)[1])
        if not all((city_block, grade, meal_block, transit_block)):
            continue
        for block in source:
            rows = _table(block)
            header = next((row for row in rows if grade in row and "职级" in row), [])
            if not header:
                continue
            col, role = header.index(grade), header.index("职级")
            row = next((row for row in rows if len(row) > max(col, role) and row[role] == title), None)
            if row and re.fullmatch(r"\d+(?:\.\d+)?", row[col]):
                lodging_block, room_rate = block, Decimal(row[col])
                break
        if lodging_block is None:
            continue
        lodging = room_rate * int(nights[1])
        food = food_rate * int(days[1]) - reduction * int(meals[1])
        transit = transit_rate * int(days[1])
        if food < 0:
            continue
        totals.append(
            f"合计最多 **{_number(lodging + food + transit)} 元**。\n\n"
            f"- 住宿费：{city}属于{grade}，{title}按 {_number(room_rate)} 元/晚 × {nights[1]} 晚"
            f" = {_number(lodging)} 元 {_ref(city_block)}{_ref(lodging_block)}。\n"
            f"- 伙食补助：{_number(food_rate)} 元/天 × {days[1]} 天 − {_number(reduction)} 元/餐 × {meals[1]} 餐"
            f" = {_number(food)} 元 {_ref(meal_block)}。\n"
            f"- 市内交通费：{_number(transit_rate)} 元/天 × {days[1]} 天"
            f" = {_number(transit)} 元 {_ref(transit_block)}。")
    return totals[0] if len(set(totals)) == 1 else ""


def _directory(question, evidence):
    title = re.search(r"《([^》]+)》", question)
    if not title or "文号" not in question or not re.search(r"归口|部门", question):
        return ""
    for block in evidence:
        cells = [_compact(cell) for cell in block.get("text", "").split("|")]
        if f"《{title[1]}》" not in cells:
            continue
        number = next((cell for cell in cells if re.fullmatch(r"[\u4e00-\u9fff]{2,12}〔\d{4}〕\d+号", cell)), "")
        office = cells[-1]
        if number and re.fullmatch(r"[\u4e00-\u9fff]{2,18}(?:部|办公室|中心)", office):
            return f"《{title[1]}》的文号是 **{number}**，归口部门是**{office}** {_ref(block)}。"
    return ""


def _diagram(question, evidence):
    step = re.search(r"(?:步骤|节点)(\d{1,4})", question)
    symbols = list(dict.fromkeys(re.findall(r"[△○□◎●◇◆]", question)))
    if not step or not symbols:
        return ""
    for block in evidence:
        diagram = block.get("diagram")
        if not diagram or diagram["step"] != step[1]:
            continue
        legend = _compact(diagram["legend"])
        meanings = [re.search(re.escape(symbol) + r"([\u4e00-\u9fff]{2,12})", legend) for symbol in symbols]
        if all(meanings):
            definitions = "，".join(f"`{symbol}` 表示{meaning[1]}" for symbol, meaning in zip(symbols, meanings))
            return f"{definitions}。步骤 {step[1]} 是**{diagram['action']}** {_ref(block)}。"
    return ""


def _report_location(question, evidence):
    if "设计回访" not in question or not PAGE_QUESTION.search(question):
        return ""
    asks_deadline = "回访报告" in question and re.search(
        r"多久|多长时间|时限|期限|几[天日]|多少[天日]|何时|什么时候|内完成", question)
    asks_toc = re.search(r"目录|原(?:始)?Word", question, re.I)
    asks_footer = re.search(r"页脚|印刷页码|印着|印的页码", question)
    asks_jump = re.search(r"翻到|跳到|从文件开头", question)
    asks_pdf = asks_jump or re.search(r"PDF|阅读器", question, re.I)
    for block in evidence:
        location = block.get("location", {})
        if (_compact(location.get("heading", "")) != REPORT_HEADING
                or not location.get("starts_section") or block.get("source_id") is None):
            continue
        deadline = None
        if asks_deadline:
            deadline = re.search(
                r"中型工程[^。；]{0,35}?在(\d+天)内完成[，,]大型工程[^。；]{0,35}?在(\d+个月)内完成",
                _compact(block.get("text", "")),
            )
            if not deadline:
                continue
        pdf_page = location["pdf_page"]
        printed = location.get("printed_page")
        converted_word = location.get("source_format") in {".doc", ".docx"}
        pdf = f"在服务器 PDF 中，“设计回访”位于 **PDF 第 {pdf_page} 页**"
        if printed:
            pdf += f"（页脚标为 {printed}）"
        pdf += f" {_ref(block)}。"
        toc = ""
        difference = ""
        for index in evidence:
            if index.get("source_id") != block.get("source_id"):
                continue
            entry = index.get("contents", {}).get("4.10")
            if not entry:
                continue
            label = "原 Word 目录" if converted_word else "目录"
            toc = f"{label}标注“4.10 设计回访”在 **第 {entry} 页** {_ref(index)}。"
            if converted_word and printed and str(entry) != printed:
                difference = "目录沿用原 Word 的页码，PDF 的跳转位置以服务器转换后的分页为准。"
            break
        if asks_footer and not asks_toc and printed:
            answer = (f"服务器 PDF 的**页脚标为 {printed}**，阅读器里请跳到 "
                      f"**PDF 第 {pdf_page} 页** {_ref(block)}。" + toc)
        elif asks_pdf and (asks_jump or not asks_toc):
            answer = pdf + toc
        else:
            answer = toc + pdf
        if difference:
            answer += "\n\n" + difference
        if deadline:
            answer += (f"\n\n中型工程回访报告在 **{deadline[1]}**内完成，"
                       f"大型工程在 **{deadline[2]}**内完成 {_ref(block)}。")
        return answer
    return ""


def _seal_rule(question, evidence):
    urgent = all(term in question for term in ("出图", "合同", "加急", "用印"))
    retention = all(term in question for term in ("纸质", "用印登记簿", "保存"))
    if not (urgent or retention) or re.search(r"旧版|(?:19|20)\d{2}(?:年|版)", question):
        return ""
    for block in evidence:
        text = re.sub(r"[·•]", "。", _compact(block.get("text", "")))
        if urgent:
            clause = re.search(r"(第[一二三四五六七八九十\d]+条)出图章、合同章用印[：:]([^。]+。)", text)
            if clause and "加急" in clause[2]:
                return f"按{clause[1]}，{clause[2]} {_ref(block)}"
        if retention:
            clause = re.search(r"(第[一二三四五六七八九十\d]+条)用印完成后[^\n]*", text)
            if not clause:
                continue
            paper = re.search(r"纸质(?:《用印登记簿》|用印登记簿)不再使用", clause[0])
            scan = re.search(r"(?:用印)?文件扫描件(?:保存期)?不少于(\d+)年", clause[0])
            if (paper and scan and "OA用印台账永久保存" in clause[0]
                    and re.search(r"形成(?:电子|用印)台账", clause[0])):
                return (f"纸质《用印登记簿》不再使用。用印后在 OA 确认并上传扫描件，由系统生成电子台账。"
                        f"OA 台账**永久保存**，用印文件扫描件保存**不少于 {scan[1]} 年**。"
                        f"依据{clause[1]} {_ref(block)}。")
    return ""


def policy_answer(question: str, evidence: list[dict]) -> str:
    question = _compact(question)
    return (_change(question, evidence) or _mileage(question, evidence)
            or _trip_total(question, evidence) or _directory(question, evidence)
            or _diagram(question, evidence) or _report_location(question, evidence) or _seal_rule(question, evidence))
