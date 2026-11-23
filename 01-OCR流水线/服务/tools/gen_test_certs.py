"""自造测试证件生成器 —— 全部虚构数据，固定随机种子，可复现。

用法（服务目录）：
    .venv/Scripts/python.exe tools/gen_test_certs.py [--out ../资料/自造测试集] [--seed 20261118]

产出：
    <out>/id_front_00_scan.jpg ...        每类 >=10 张，文件名含扰动类型
    <out>/标准答案.json                    与公开样例同构，另含 invalid_fields 标记
    <out>/生成参数.json                    每张图的扰动参数（角度/位置/强度）

覆盖条件：scan 正常扫描 / rotate 旋转 / perspective 翻拍透视 / blur 模糊 /
noise 噪点 / glare 反光 / uneven 光照不均 / watermark 水印 / wrap 地址换行 /
bad_number 号码校验位错误 / bad_date 日期非法 / bad_period 有效期倒挂 / long_term 长期。
生成后用 app.certs.validators 自检：合法号码必须通过；故意错的仅 USCC/日期仍会被抓
（身份证校验位规则已取消，bad_number 的身份证号只是数据层面"错"，不再判失败）。
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.certs.validators import validate_chinese_id, validate_uscc  # noqa: E402

# ---------------------------------------------------------------- 虚构数据池

SURNAMES = "赵钱孙李周吴郑王冯陈蒋沈韩杨朱秦许吕施张孔曹严金魏陶姜"
GIVEN = ["伟", "芳", "娜", "秀英", "敏", "静", "丽", "磊", "军", "洋", "勇",
         "艳", "杰", "涛", "明超", "霞", "桂英", "建国", "雨萱", "子墨", "清和", "雅筠"]
NATIONS = ["汉", "壮", "回", "满", "苗"]
GENDERS = ["男", "女"]
ADDRESSES = [
    "示例省示例市滨河区望江路128号锦绣花园7栋2单元603室",
    "示例自治区示例市青云县翠屏镇东风街道幸福里巷46号",
    "示例市朝阳区科创路8号数字大厦15层1503室",
    "示例省临江县白沙镇文化街33号",
]
ISSUERS = ["示例市公安局示例分局", "示例市公安局", "示例市公安局澄川派出所"]
COMPANY_PREFIX = ["星河", "江澜", "云脊", "沧月", "天工", "铭川", "青梧", "岚图"]
COMPANY_SUFFIX = ["智能科技", "文化传媒", "建筑装饰", "供应链管理", "信息技术"]
COMPANY_FORM = ["有限公司", "股份有限公司"]
LICENSE_TYPES = ["有限责任公司（自然人投资或控股）", "其他股份有限公司（非上市）",
                 "有限责任公司（自然人独资）"]
CAPITALS = ["伍佰万圆整", "壹仟贰佰万圆整", "叁佰万圆整", "贰仟万圆整"]
SCOPES = [
    "人工智能应用软件开发；信息系统集成服务；数据处理和存储支持服务；技术咨询、技术转让；计算机软硬件销售（依法须经批准的项目，经相关部门批准后方可开展经营活动）",
    "组织文化艺术交流活动；广告设计、代理；图文设计制作；会议及展览服务；出版物零售（依法须经批准的项目，经相关部门批准后方可开展经营活动）",
    "建筑工程设计；工程管理服务；规划设计管理；专业设计服务（依法须经批准的项目，经相关部门批准后方可开展经营活动）",
]
SCOPES_SHORT = ["建材销售；五金零售", "餐饮管理；食品销售（不含预制菜）"]
PERMIT_CLASSES = ["C1", "C2", "B2", "A2", "B1"]

_REGION = "999901"  # 虚构地区码，与公开样例同段
_ID_WEIGHTS = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
_ID_CHECK = "10X98765432"
_USCC_CHARS = "0123456789ABCDEFGHJKLMNPQRTUWXY"
_USCC_WEIGHTS = [1, 3, 9, 27, 19, 26, 16, 17, 20, 29, 25, 13, 8, 24, 10, 30, 28]


def _id_check_char(first17: str) -> str:
    total = sum(int(first17[i]) * _ID_WEIGHTS[i] for i in range(17))
    return _ID_CHECK[total % 11]


def make_id_number(rng: random.Random, birth: date, gender: str,
                   broken: bool = False) -> str:
    """生成虚构身份证号（地区+生日+顺序码+校验位）。broken=True 时校验位故意错。"""
    seq = f"{rng.randrange(1, 999):03d}"
    if (int(seq[2]) % 2 == 1) != (gender == "男"):
        seq = seq[:2] + ("1" if gender == "男" else "2")  # 第17位奇偶与性别一致
    first17 = f"{_REGION}{birth.strftime('%Y%m%d')}{seq}"
    check = _id_check_char(first17)
    if broken:
        wrong = next(c for c in _ID_CHECK if c != check)
        number = first17 + wrong
        # 校验位规则已取消：broken 号码结构合法即通过，仅作为评测"漏检"样例保留
        assert validate_chinese_id(number)[0], number
        return number
    number = first17 + check
    assert validate_chinese_id(number)[0], number
    return number


def make_uscc(rng: random.Random, broken: bool = False) -> str:
    body = "91" + "".join(rng.choice(_USCC_CHARS) for _ in range(15))
    total = sum(_USCC_CHARS.index(c) * _USCC_WEIGHTS[i] for i, c in enumerate(body))
    check = _USCC_CHARS[(31 - total % 31) % 31]
    code = body + (next(c for c in _USCC_CHARS if c != check) if broken else check)
    assert validate_uscc(code)[0] is (not broken)
    return code


def _cn_date(d: date) -> str:
    return f"{d.year}年{d.month}月{d.day}日"       # 与公开样例一致：月日不补零


def _dot_date(d: date) -> str:
    return d.strftime("%Y.%m.%d")


# ---------------------------------------------------------------- 渲染

def _load_font(size: int):
    from PIL import ImageFont

    for name in ("msyh.ttc", "simhei.ttf", "simsun.ttc"):
        path = Path("C:/Windows/Fonts") / name
        if path.exists():
            return ImageFont.truetype(str(path), size)
    raise SystemExit("未找到中文字体（msyh/simhei/simsun），无法生成测试证件")


class Renderer:
    def __init__(self):
        self.title_font = _load_font(40)
        self.small_title_font = _load_font(30)
        self.font = _load_font(36)      # 标签/值字号接近真实证件比例，
        self.small_font = _load_font(28)  # 过小会诱发"姓→生"类形近字误读
        self.license_font = _load_font(32)  # license 画布最宽，28px 相对过小仍误读

    def _canvas(self, width: int, height: int, color=(245, 246, 240)):
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (width, height), color)
        return image, ImageDraw.Draw(image)

    def _title(self, draw, text: str, width: int, y: int, font=None):
        font = font or self.title_font
        bbox = draw.textbbox((0, 0), text, font=font)
        draw.text(((width - bbox[2] + bbox[0]) / 2, y), text, fill=(90, 30, 30), font=font)

    def _rows(self, draw, rows: list[tuple], x_label: int, y0: int, step: int,
              label_font=None, value_font=None, value_dx: int = 130):
        """rows: (label, value) 同行左右排；value 为 list 时为多行（首行同行，其余下移）。"""
        label_font = label_font or self.font
        value_font = value_font or self.font
        y = y0
        for label, value in rows:
            lines = value if isinstance(value, list) else [value]
            draw.text((x_label, y), label, fill=(40, 40, 40), font=label_font)
            for index, line in enumerate(lines):
                draw.text((x_label + value_dx, y + index * step), line,
                          fill=(20, 20, 20), font=value_font)
            y += step * len(lines)
        return y

    def id_front(self, data: dict):
        # 左边距 ≥110px：预处理（UVDoc 摆正）会裁掉画布边缘，标签贴边会被切掉
        # 最左字符的偏旁（姓名→生名、住址→主址皆由此而来）
        image, draw = self._canvas(980, 620)
        self._title(draw, "中华人民共和国居民身份证", 980, 36, self.small_title_font)
        rows = [("姓名", data["姓名"]), ("性别", data["性别"]), ("民族", data["民族"]),
                ("出生", data["出生日期"]), ("住址", data["住址_lines"])]
        end = self._rows(draw, rows, 120, 120, 56, value_dx=170)
        number_y = max(end + 20, 520)
        draw.text((120, number_y), "公民身份号码", fill=(40, 40, 40), font=self.font)
        draw.text((370, number_y), data["公民身份号码"],
                  fill=(20, 20, 20), font=self.font)
        return image

    def id_back(self, data: dict):
        image, draw = self._canvas(980, 620)
        self._title(draw, "中华人民共和国", 980, 50, self.small_title_font)
        # 真证版式：值行在标签正下方且与标签有横向重叠（next_line 提取依赖 x 重叠）
        draw.text((120, 200), "签发机关", fill=(40, 40, 40), font=self.font)
        draw.text((130, 258), data["签发机关"], fill=(20, 20, 20), font=self.font)
        draw.text((120, 400), "有效期限", fill=(40, 40, 40), font=self.font)
        draw.text((310, 400), data["有效期限"], fill=(20, 20, 20), font=self.font)
        return image

    def license(self, data: dict):
        image, draw = self._canvas(1320, 900)
        self._title(draw, "营业执照", 1320, 36)
        rows = [("统一社会信用代码", data["统一社会信用代码"]),
                ("名称", data["名称"]), ("类型", data["类型"]),
                ("法定代表人", data["法定代表人"]), ("注册资本", data["注册资本"]),
                ("成立日期", data["成立日期"]), ("住所", data["住所_lines"]),
                ("经营范围", data["经营范围_lines"])]
        self._rows(draw, rows, 130, 150, 58, self.license_font, self.license_font,
                   value_dx=340)
        return image

    def driver(self, data: dict):
        image, draw = self._canvas(980, 700)
        self._title(draw, "中华人民共和国机动车驾驶证", 980, 30, self.small_title_font)
        rows = [("证号", data["证号"]), ("姓名", data["姓名"]), ("性别", data["性别"]),
                ("国籍", data["国籍"]), ("出生日期", data["出生日期"]),
                ("住址", data["住址_lines"]), ("初次领证日期", data["初次领证日期"]),
                ("准驾车型", data["准驾车型"]), ("有效期限", data["有效期限"])]
        self._rows(draw, rows, 110, 100, 54, self.small_font, self.small_font,
                   value_dx=210)
        return image


# ---------------------------------------------------------------- 扰动

def _scan_baseline(image, rng):
    """所有条件共有的轻度'扫描感'：降采样再放大 + 轻噪点。"""
    import numpy as np

    small = image.resize((int(image.width * 0.72), int(image.height * 0.72)))
    image = small.resize(image.size)
    array = np.asarray(image).astype(np.int16)
    array += rng.randint(-6, 6)
    from PIL import Image

    return Image.fromarray(np.clip(array, 0, 255).astype(np.uint8))


def perturb(image, cond: str, rng: random.Random) -> tuple:
    """按条件劣化图像，返回 (新图, 参数记录)。全部先过扫描基线。"""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter

    params: dict = {}
    image = _scan_baseline(image, rng)
    if cond == "scan":
        pass
    elif cond == "rotate":
        params["angle"] = round(rng.uniform(3, 8) * rng.choice([1, -1]), 1)
        image = image.rotate(params["angle"], expand=False, fillcolor=(255, 255, 255),
                             resample=Image.BICUBIC)
    elif cond == "perspective":  # 翻拍：梯形透视（上窄下宽的 keystone 形变）
        dx = rng.randint(26, 44)
        params["shift"] = dx
        width, height = image.size
        # PIL QUAD 顺序：源四边形 左上、左下、右下、右上
        source = (dx, 0, 0, height, width, height, width - dx, 0)
        image = image.transform(image.size, Image.QUAD, source, resample=Image.BICUBIC,
                                fillcolor=(255, 255, 255))
        image = image.filter(ImageFilter.GaussianBlur(0.8))
    elif cond == "blur":
        params["radius"] = 2.4
        image = image.filter(ImageFilter.GaussianBlur(params["radius"]))
    elif cond == "noise":
        params["sigma"] = 16
        noise_rng = np.random.default_rng(rng.randrange(1 << 30))  # 种子派生自主 rng，可复现
        array = np.asarray(image).astype(np.int16)
        array += noise_rng.normal(0, params["sigma"], array.shape).astype(np.int16)
        image = Image.fromarray(np.clip(array, 0, 255).astype(np.uint8))
    elif cond == "glare":  # 反光：白色椭圆光斑
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        cx, cy = rng.randint(200, 700), rng.randint(100, 400)
        params["center"] = [cx, cy]
        draw.ellipse((cx - 150, cy - 80, cx + 150, cy + 80), fill=(255, 255, 255, 130))
        image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
    elif cond == "uneven":  # 光照不均：横向亮度渐变
        params["low"] = 0.55
        array = np.asarray(image).astype(np.float32)
        gradient = np.linspace(params["low"], 1.05, array.shape[1])[None, :, None]
        image = Image.fromarray(np.clip(array * gradient, 0, 255).astype(np.uint8))
    elif cond == "watermark":  # 与'样本'水印戳同款，验证 stop_words 清洗
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        stamp = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(stamp)
        draw.text((image.width // 2 - 90, image.height // 2 - 45), "样本",
                  fill=(210, 30, 30, 110), font=_load_font(90))
        stamp = stamp.rotate(18, expand=False)
        overlay.alpha_composite(stamp)
        image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
    else:  # wrap / bad_number / bad_date / bad_period / long_term 不做几何/画质劣化
        pass
    return image, params


# ---------------------------------------------------------------- 数据与布局

def _wrap_address(address: str, first_limit: int) -> list[str]:
    return [address[:first_limit], address[first_limit:]]


def make_record(doc: str, rng: random.Random, cond: str) -> tuple[dict, list[str]]:
    """生成一份虚构证件数据与对应字段表。返回 (渲染数据, 标准答案 fields)。"""
    gender = rng.choice(GENDERS)
    birth = date(rng.randrange(1962, 2004), rng.randrange(1, 13), rng.randrange(1, 29))
    name = rng.choice(SURNAMES) + rng.choice(GIVEN)
    address = rng.choice(ADDRESSES)
    invalid: list[str] = []

    if doc == "id_front":
        bad_number = cond == "bad_number"
        number = make_id_number(rng, birth, gender, broken=bad_number)
        if bad_number:
            invalid.append("公民身份号码")
        birth_text = "2021年2月30日" if cond == "bad_date" else _cn_date(birth)
        if cond == "bad_date":
            invalid.append("出生日期")
            number = number[:6] + "20210230" + number[14:]  # 生日段非法，校验应抓
        # 真证住址必折行且不顶满画布（单行满宽会在旋转/透视下被裁掉尾部）
        address_lines = (_wrap_address(address + rng.choice(ADDRESSES), 15)
                         if cond == "wrap" else _wrap_address(address, 15))
        data = {"姓名": name, "性别": gender, "民族": rng.choice(NATIONS),
                "出生日期": birth_text, "住址_lines": address_lines,
                "公民身份号码": number}
        fields = {"姓名": name, "性别": gender, "民族": data["民族"],
                  "出生日期": birth_text, "住址": "".join(address_lines),
                  "公民身份号码": number}
    elif doc == "id_back":
        start = date(rng.randrange(2018, 2023), rng.randrange(1, 13), rng.randrange(1, 29))
        end = date(start.year + rng.randrange(10, 21), start.month, start.day)
        if cond == "long_term":
            period = f"{_dot_date(start)}-长期"
        elif cond == "bad_period":
            period = f"{_dot_date(end)}-{_dot_date(start)}"  # 起止倒挂
            invalid.append("有效期限")
        else:
            period = f"{_dot_date(start)}-{_dot_date(end)}"
        issuer = rng.choice(ISSUERS)
        data = {"签发机关": issuer, "有效期限": period}
        fields = {"签发机关": issuer, "有效期限": period}
    elif doc == "license":
        bad_number = cond == "bad_number"
        uscc = make_uscc(rng, broken=bad_number)
        if bad_number:
            invalid.append("统一社会信用代码")
        found = date(rng.randrange(2008, 2024), rng.randrange(1, 13), rng.randrange(1, 29))
        found_text = "2021年2月30日" if cond == "bad_date" else _cn_date(found)
        if cond == "bad_date":
            invalid.append("成立日期")
        company = f"示例{rng.choice(COMPANY_PREFIX)}{rng.choice(COMPANY_SUFFIX)}{rng.choice(COMPANY_FORM)}"
        scope = rng.choice(SCOPES if cond == "wrap" else SCOPES_SHORT)
        scope_lines = ([scope[:26], scope[26:52], scope[52:]] if cond == "wrap"
                       else [scope])
        address_lines = _wrap_address(address, 20)
        data = {"统一社会信用代码": uscc, "名称": company,
                "类型": rng.choice(LICENSE_TYPES), "法定代表人": name,
                "注册资本": rng.choice(CAPITALS), "成立日期": found_text,
                "住所_lines": address_lines, "经营范围_lines": scope_lines}
        fields = {"统一社会信用代码": uscc, "名称": company, "类型": data["类型"],
                  "法定代表人": name, "经营范围": "".join(scope_lines),
                  "注册资本": data["注册资本"], "成立日期": found_text,
                  "住所": "".join(address_lines)}
    else:  # driver
        bad_number = cond == "bad_number"
        number = make_id_number(rng, birth, gender, broken=bad_number)
        if bad_number:
            invalid.append("证号")
        birth_text = "2021年2月30日" if cond == "bad_date" else _cn_date(birth)
        if cond == "bad_date":
            invalid.append("出生日期")
            number = number[:6] + "20210230" + number[14:]
        issue = date(rng.randrange(2014, 2023), rng.randrange(1, 13), rng.randrange(1, 29))
        valid_end = date(issue.year + rng.randrange(6, 11), issue.month, issue.day)
        address_lines = (_wrap_address(address + rng.choice(ADDRESSES), 15)
                         if cond == "wrap" else _wrap_address(address, 15))
        data = {"证号": number, "姓名": name, "性别": gender, "国籍": "中国",
                "出生日期": birth_text, "住址_lines": address_lines,
                "初次领证日期": _cn_date(issue), "准驾车型": rng.choice(PERMIT_CLASSES),
                "有效期限": f"{_dot_date(issue)}-{_dot_date(valid_end)}"}
        fields = {"证号": number, "姓名": name, "性别": gender, "国籍": "中国",
                  "住址": "".join(address_lines), "出生日期": birth_text,
                  "初次领证日期": _cn_date(issue), "准驾车型": data["准驾车型"],
                  "有效期限": data["有效期限"]}
    return data, fields, invalid


DOC_CN = {"id_front": "身份证正面", "id_back": "身份证反面",
          "license": "营业执照", "driver": "驾驶证"}

#: 每类条件池（全部 >=10）；不适用的条件自动换补 scan/rotate
CONDITIONS = {
    "id_front": ["scan", "rotate", "perspective", "blur", "noise", "glare", "uneven",
                 "watermark", "wrap", "bad_number", "bad_date"],
    "id_back": ["scan", "rotate", "perspective", "blur", "noise", "glare", "uneven",
                "watermark", "long_term", "bad_period"],
    "license": ["scan", "rotate", "perspective", "blur", "noise", "glare", "uneven",
                "watermark", "wrap", "bad_number", "bad_date"],
    "driver": ["scan", "rotate", "perspective", "blur", "noise", "glare", "uneven",
               "watermark", "wrap", "bad_number", "bad_date"],
}
FILLERS = ["scan", "rotate", "glare", "blur"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT.parent / "资料" / "自造测试集")
    parser.add_argument("--seed", type=int, default=20261118)
    args = parser.parse_args()
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    renderer = Renderer()

    truth, params_log = [], []
    for doc, conds in CONDITIONS.items():
        rng = random.Random(f"{args.seed}:{doc}")
        plan = conds + [FILLERS[i % len(FILLERS)]
                        for i in range(max(0, 10 - len(conds)))]
        for index, cond in enumerate(plan):
            data, fields, invalid = make_record(doc, rng, cond)
            image = getattr(renderer, doc)(data)
            image, params = perturb(image, cond, rng)
            filename = f"{doc}_{index:02d}_{cond}.jpg"
            image.save(out / filename, quality=90)
            truth.append({"file": filename, "doc": doc, "doc_cn": DOC_CN[doc],
                          "cond": cond, "fields": fields,
                          **({"invalid_fields": invalid} if invalid else {})})
            params_log.append({"file": filename, "cond": cond,
                               "params": params, "seed_key": f"{args.seed}:{doc}"})
            print(f"{filename}  invalid={invalid or '-'}")

    (out / "标准答案.json").write_text(json.dumps(truth, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    (out / "生成参数.json").write_text(json.dumps(params_log, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    print(f"\n共 {len(truth)} 张 → {out}")
    per_type = {kind: sum(1 for t in truth if t["doc"] == kind) for kind in CONDITIONS}
    assert all(count >= 10 for count in per_type.values()), per_type
    print("每类张数：", per_type)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
