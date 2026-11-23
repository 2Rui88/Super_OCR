# -*- coding: utf-8 -*-
"""页码域后处理：按节的 pgNumType 格式给对应页脚 PAGE 域加显式格式开关（WPS 兼容），
并清除封面节的空 <w:pgNumType/>。"""
import re
import shutil
import sys
import zipfile
from pathlib import Path

path = Path(sys.argv[1])
tmp = path.with_suffix(".tmp.docx")

with zipfile.ZipFile(path) as zin:
    names = zin.namelist()
    data = {n: zin.read(n) for n in names}

doc = data["word/document.xml"].decode("utf-8")
rels = data["word/_rels/document.xml.rels"].decode("utf-8")

# rId → 页脚文件名
rid2file = dict(re.findall(r'<Relationship[^>]*Id="([^"]+)"[^>]*Target="(footer\d+\.xml)"', rels))
rid2file.update({rid: f for f, rid in re.findall(
    r'<Relationship[^>]*Target="(footer\d+\.xml)"[^>]*Id="([^"]+)"', rels) for f, rid in [(f, rid)]} )

# 逐个 sectPr：看 pgNumType 的 fmt，决定该节页脚的格式开关
fmt_map = {}  # footer file -> roman|arabic
for sect in re.findall(r"<w:sectPr[ >].*?</w:sectPr>", doc, flags=re.S):
    fmt_m = re.search(r'<w:pgNumType[^>]*w:fmt="([^"]+)"', sect)
    for rid in re.findall(r'<w:footerReference[^>]*r:id="([^"]+)"', sect):
        f = rid2file.get(rid)
        if not f or not fmt_m:
            continue
        if fmt_m.group(1) == "upperRoman":
            fmt_map[f] = "ROMAN"
        elif fmt_m.group(1) in ("decimal", "lowerRoman", "arabic"):
            fmt_map[f] = "arabic"

patched = []
for fname, switch in fmt_map.items():
    key = f"word/{fname}"
    if key not in data:
        continue
    xml = data[key].decode("utf-8")
    new_xml, n = re.subn(
        r"(<w:instrText[^>]*>)\s*PAGE\s*(</w:instrText>)",
        rf"\1 PAGE \\* {switch} \\* MERGEFORMAT \2", xml)
    if n:
        data[key] = new_xml.encode("utf-8")
        patched.append(f"{fname} -> {switch} ({n} field)")

# 清除空 pgNumType（封面节）
doc2, removed = re.subn(r"<w:pgNumType/>", "", doc)
data["word/document.xml"] = doc2.encode("utf-8")

with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
    for n in names:
        zout.writestr(n, data[n])
shutil.move(tmp, path)
print(f"patched footers: {patched or 'none'}; empty pgNumType removed: {removed}")
