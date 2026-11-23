# -*- coding: utf-8 -*-
"""为《企业数字化转型项目年度报告》生成两张配图（DM-1 深青配色派生，Morandi 低饱和）。"""
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager

FONT = font_manager.FontProperties(fname="C:/Windows/Fonts/simhei.ttf", size=11)
FONT_SMALL = font_manager.FontProperties(fname="C:/Windows/Fonts/simhei.ttf", size=9)
ACCENT = "#1B6B7A"      # DM-1 深化 accent（白底表格同源色）
ACCENT_LIGHT = "#7FB3BC"
GREY = "#8A9AA5"
OUT = Path(__file__).parent

# 图1：分季度投资完成额（柱状图）
quarters = ["一季度", "二季度", "三季度", "四季度"]
plan = [860, 1020, 940, 1180]
actual = [820, 1055, 915, 1210]
x = range(len(quarters))
fig, ax = plt.subplots(figsize=(8, 4.2), dpi=150)
w = 0.36
ax.bar([i - w / 2 for i in x], plan, width=w, label="计划投资（万元）", color=GREY)
ax.bar([i + w / 2 for i in x], actual, width=w, label="完成投资（万元）", color=ACCENT)
for i, v in enumerate(actual):
    ax.text(i + w / 2, v + 18, str(v), ha="center", fontproperties=FONT_SMALL, color="#333333")
ax.set_xticks(list(x))
ax.set_xticklabels(quarters, fontproperties=FONT)
ax.set_ylabel("金额（万元）", fontproperties=FONT)
ax.set_ylim(0, 1400)
ax.spines[["top", "right"]].set_visible(False)
ax.grid(axis="y", linestyle="--", alpha=0.35)
ax.legend(prop=FONT, frameon=False, loc="upper left")
fig.tight_layout()
fig.savefig(OUT / "chart_quarterly.png")
plt.close(fig)

# 图2：月度线上工单量与平均处理时长（折线图）
months = [f"{m}月" for m in range(1, 13)]
tickets = [1240, 1180, 1365, 1502, 1620, 1715, 1650, 1782, 1920, 2050, 2145, 2280]
hours = [26.5, 25.8, 24.2, 23.5, 21.8, 20.4, 19.6, 18.2, 17.5, 16.8, 15.9, 14.6]
fig, ax1 = plt.subplots(figsize=(8, 4.2), dpi=150)
ax1.plot(months, tickets, marker="o", color=ACCENT, label="线上工单量（件）")
ax1.set_ylabel("工单量（件）", fontproperties=FONT, color=ACCENT)
ax1.tick_params(axis="y", labelcolor=ACCENT)
ax1.set_ylim(1000, 2500)
ax2 = ax1.twinx()
ax2.plot(months, hours, marker="s", linestyle="--", color=GREY, label="平均处理时长（小时）")
ax2.set_ylabel("平均处理时长（小时）", fontproperties=FONT, color="#6B7A85")
ax2.tick_params(axis="y", labelcolor="#6B7A85")
ax2.set_ylim(10, 30)
ax1.set_xticks(range(len(months)))
ax1.set_xticklabels(months, fontproperties=FONT_SMALL)
ax1.spines[["top"]].set_visible(False)
ax2.spines[["top"]].set_visible(False)
lines = ax1.get_lines() + ax2.get_lines()
ax1.legend(lines, [l.get_label() for l in lines], prop=FONT_SMALL, frameon=False, loc="upper left")
ax1.grid(axis="y", linestyle="--", alpha=0.3)
fig.tight_layout()
fig.savefig(OUT / "chart_monthly.png")
plt.close(fig)
print("charts done:", OUT / "chart_quarterly.png", OUT / "chart_monthly.png")
