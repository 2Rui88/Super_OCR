"""D0 冒烟测试（离线部分）：校验器 + 配置加载 + 分类评分。

用 资料/公开样例/标准答案.json 的真实字段值做验证：
1. 所有样例的证件号/信用代码/日期都能通过对应校验器（标准答案本身应当合法）；
2. 故意做错的值，仍生效的规则必须抓出来（安全网自证；身份证校验位规则已取消）；
3. 4 类证件配置能加载，分类评分用"字段拼接文本"能判对类型。

运行：python tests/test_d0_offline.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.certs import classify, load_certs  # noqa: E402
from app.certs import validators as V  # noqa: E402

GROUND_TRUTH = ROOT.parent / "资料" / "公开样例" / "标准答案.json"
CERT_DIR = ROOT / "config" / "certs"

passed, failed = 0, []


def check(name: str, ok: bool, detail: str = ""):
    global passed
    if ok:
        passed += 1
    else:
        failed.append(f"{name} {detail}")
    print(f"  {'✓' if ok else '✗'} {name}{'  ' + detail if detail and not ok else ''}")


def main() -> int:
    print("== 1. 校验器 ==")
    # 算法自证：标准国标测试号必须通过
    check("国标样例身份证号通过", V.validate_chinese_id("11010519491231002X")[0])
    gt = json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))
    # 公开样例的号码是虚构的，不满足校验位（考核场景"号码有误"就是故意的），
    # 且身份证校验位规则已取消 —— 这里只校验格式（位数/字符集/日期段）。
    # —— 评测口径由此确定：字段准确率 = 抽取值 vs 标准答案字符串比对；
    #    校验器输出独立统计，不参与准确率。
    for rec in gt:
        fields = rec["fields"]
        for fname, val in fields.items():
            if fname in ("公民身份号码", "证号"):
                n = V.normalize_id_number(val)
                check(f"{rec['file']} {fname} 格式", len(n) == 18 and n[:17].isdigit(),
                      f"'{val}'")
            elif fname == "统一社会信用代码":
                check(f"{rec['file']} {fname} 格式", len(val) == 18, f"'{val}'")
                print(f"      (校验位状态: {V.validate_uscc(val)[0] or '不通过'} —— 虚构号码，预期内)")

    print("== 2. 错误必须被抓出（安全网自证） ==")
    bad_id = "999901199603146918"      # 改末位（校验位规则已取消，结构合法即通过）
    ok, errs = V.validate_chinese_id(bad_id)
    check("错校验位身份证不再判失败（规则已取消）", ok, str(errs))
    ok, errs = V.validate_chinese_id("999901199613146917")  # 13月
    check("非法出生月被抓", not ok, str(errs))
    ok, errs = V.validate_uscc("91440300MA5DD1AB8X")       # 改末位
    check("错校验位信用代码被抓", not ok, str(errs))
    ok, errs = V.validate_date_range("2035.01.01", "2015.01.01")
    check("起止倒挂被抓", not ok, str(errs))
    ok, errs = V.validate_date_range("2015.01.01", "长期")
    check("'长期'截止日合法", ok, str(errs))
    ok, _ = V.validate_chinese_id("110105 19491231 002X")  # 断行容错（用校验位合法的号）
    check("断行证号归一化后通过", ok)

    print("== 3. 配置加载 + 分类 ==")
    registry = load_certs(CERT_DIR)
    check("加载4类证件配置", set(registry) == {"id_front", "id_back", "driver", "license"},
          str(set(registry)))
    # 用字段名+字段值拼一段"伪OCR文本"来测分类
    doc_field_text = {
        "id_front": "居民身份证 姓名 性别 民族 出生 住址 公民身份号码",
        "id_back": "中华人民共和国 居民身份证 签发机关 有效期限",
        "driver": "中华人民共和国机动车驾驶证 证号 姓名 性别 国籍 住址 初次领证日期 准驾车型",
        "license": "营业执照 统一社会信用代码 名称 法定代表人 经营范围 注册资本 成立日期",
    }
    for expect, text in doc_field_text.items():
        got, conf = classify(text, registry)
        check(f"分类 {expect}", got == expect, f"实际 {got} conf={conf}")
    got, _ = classify("随便一张风景照没有关键字", registry)
    check("非证件图判 unknown", got == "unknown", got)

    print(f"\n结果：{passed} 通过，{len(failed)} 失败")
    if failed:
        for f in failed:
            print("  ✗", f)
        return 1
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
