/* 企业数字化转型年度报告 —— 长复杂 docx 生成（docx-js）
 * 结构：R1 封面(DM-1) → 前置区(摘要+目录, 罗马页码) → 正文区(8章+2附录, 阿拉伯页码)
 * 内容全部虚构（示例集团），与 OCR 测试集同风格。
 */
const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  ImageRun, PageBreak, Header, Footer, PageNumber, NumberFormat,
  AlignmentType, HeadingLevel, WidthType, BorderStyle, ShadingType,
  TableLayoutType, SectionType, LevelFormat, TableOfContents,
} = require("docx");
const fs = require("fs");

/* ---------------- 调色板 DM-1 Deep Cyan ---------------- */
const P = {
  bg: "162235", accent: "37DCF2",
  titleColor: "FFFFFF", subtitleColor: "B0B8C0", metaColor: "90989F", footerColor: "687078",
  headPrimary: "16324A",                     // 正文标题深青
  table: { headerBg: "1B6B7A", headerText: "FFFFFF", accentLine: "1B6B7A", innerLine: "C8DDE2" },
};

const NB = { style: BorderStyle.NONE, size: 0, color: "FFFFFF" };
const noBorders = { top: NB, bottom: NB, left: NB, right: NB };
const allNoBorders = { top: NB, bottom: NB, left: NB, right: NB, insideHorizontal: NB, insideVertical: NB };

/* ---------------- 封面布局函数（design-system 标准实现） ---------------- */
function splitTitleLines(title, charsPerLine) {
  if (title.length <= charsPerLine) return [title];
  const breakAfter = new Set([..."，。、；：！？", ..."的与和及之在于为", ..."-_—–·/", ..." \t"]);
  const lines = [];
  let remaining = title;
  while (remaining.length > charsPerLine) {
    let breakAt = -1;
    for (let i = charsPerLine; i >= Math.floor(charsPerLine * 0.6); i--) {
      if (i < remaining.length && breakAfter.has(remaining[i - 1])) { breakAt = i; break; }
    }
    if (breakAt === -1) {
      const limit = Math.min(remaining.length, Math.ceil(charsPerLine * 1.3));
      for (let i = charsPerLine + 1; i < limit; i++) {
        if (breakAfter.has(remaining[i - 1])) { breakAt = i; break; }
      }
    }
    if (breakAt === -1) {
      breakAt = charsPerLine;
      const prevChar = remaining[breakAt - 1], nextChar = remaining[breakAt];
      if (prevChar && nextChar && !breakAfter.has(prevChar) && !breakAfter.has(nextChar) &&
          /[\u4e00-\u9fff]/.test(prevChar) && /[\u4e00-\u9fff]/.test(nextChar)) breakAt -= 1;
    }
    lines.push(remaining.slice(0, breakAt).trim());
    remaining = remaining.slice(breakAt).trim();
  }
  if (remaining) lines.push(remaining);
  if (lines.length > 1 && lines[lines.length - 1].length <= 2) {
    const last = lines.pop();
    lines[lines.length - 1] += last;
  }
  return lines;
}

function calcTitleLayout(title, maxWidthTwips, preferredPt = 40, minPt = 24) {
  const charWidth = (pt) => pt * 20;
  const charsPerLine = (pt) => Math.floor(maxWidthTwips / charWidth(pt));
  let titlePt = preferredPt, lines;
  while (titlePt >= minPt) {
    const cpl = charsPerLine(titlePt);
    if (cpl < 2) { titlePt -= 2; continue; }
    lines = splitTitleLines(title, cpl);
    if (lines.length <= 3) break;
    titlePt -= 2;
  }
  if (!lines || lines.length > 3) {
    lines = splitTitleLines(title, charsPerLine(minPt));
    titlePt = minPt;
  }
  return { titlePt, titleLines: lines };
}

function calcCoverSpacing(params) {
  const { titleLineCount = 1, titlePt = 36, hasSubtitle = false, hasEnglishLabel = false,
          metaLineCount = 0, fixedHeight = 800, pageHeight = 16838,
          marginTop = 0, marginBottom = 0 } = params;
  const SAFETY = 1200;
  const usableHeight = pageHeight - marginTop - marginBottom - SAFETY;
  const titleHeight = titleLineCount * (titlePt * 23 + 200);
  const subtitleHeight = hasSubtitle ? (12 * 23 + 600) : 0;
  const englishLabelHeight = hasEnglishLabel ? (9 * 23 + 600) : 0;
  const metaHeight = metaLineCount * (10 * 23 + 100);
  const implicitParaHeight = 3 * 300;
  const contentHeight = titleHeight + subtitleHeight + englishLabelHeight + metaHeight + fixedHeight + implicitParaHeight;
  const safeRemaining = Math.max(usableHeight - contentHeight, 400);
  const FOOTER_MIN = 800;
  const rawTop = Math.floor(safeRemaining * 0.45);
  const rawBottom = Math.floor(safeRemaining * 0.45);
  const bottomSpacing = Math.max(rawBottom, FOOTER_MIN);
  const topSpacing = Math.max(rawTop - Math.max(0, FOOTER_MIN - rawBottom), 400);
  const midSpacing = Math.max(safeRemaining - topSpacing - bottomSpacing, 0);
  return { topSpacing, midSpacing, bottomSpacing };
}

/* ---------------- Recipe R1：左对齐纯段落封面 ---------------- */
function buildCoverR1(config) {
  const padL = 1200, padR = 800;
  const availableWidth = 11906 - padL - padR - 300;
  const { titlePt, titleLines } = calcTitleLayout(config.title, availableWidth, 40, 24);
  const titleSize = titlePt * 2;
  const spacing = calcCoverSpacing({
    titleLineCount: titleLines.length, titlePt,
    hasSubtitle: !!config.subtitle, hasEnglishLabel: !!config.englishLabel,
    metaLineCount: (config.metaLines || []).length, fixedHeight: 400,
  });
  const accentLeft = { style: BorderStyle.SINGLE, size: 8, color: P.accent, space: 12 };
  const children = [];
  children.push(new Paragraph({ spacing: { before: spacing.topSpacing } }));
  if (config.englishLabel) {
    children.push(new Paragraph({
      indent: { left: padL, right: padR }, spacing: { after: 500 },
      border: { bottom: { style: BorderStyle.SINGLE, size: 6, color: P.accent, space: 8 } },
      children: [new TextRun({ text: config.englishLabel.split("").join("  "), size: 18,
        color: P.accent, font: { ascii: "Calibri", eastAsia: "SimHei" }, characterSpacing: 40 })],
    }));
  }
  for (let i = 0; i < titleLines.length; i++) {
    children.push(new Paragraph({
      indent: { left: padL },
      spacing: { after: i < titleLines.length - 1 ? 100 : 300, line: Math.ceil(titlePt * 23), lineRule: "atLeast" },
      children: [new TextRun({ text: titleLines[i], size: titleSize, bold: true,
        color: P.titleColor, font: { eastAsia: "SimHei", ascii: "Arial" } })],
    }));
  }
  if (config.subtitle) {
    children.push(new Paragraph({
      indent: { left: padL }, spacing: { after: 800 },
      children: [new TextRun({ text: config.subtitle, size: 24, color: P.subtitleColor,
        font: { eastAsia: "Microsoft YaHei", ascii: "Arial" } })],
    }));
  }
  for (const line of (config.metaLines || [])) {
    children.push(new Paragraph({
      indent: { left: padL + 200 }, spacing: { after: 80 },
      border: { left: accentLeft },
      children: [new TextRun({ text: line, size: 24, color: P.metaColor,
        font: { eastAsia: "Microsoft YaHei", ascii: "Arial" } })],
    }));
  }
  children.push(new Paragraph({ spacing: { before: spacing.bottomSpacing } }));
  children.push(new Paragraph({
    indent: { left: padL, right: padR },
    border: { top: { style: BorderStyle.SINGLE, size: 2, color: P.accent, space: 8 } },
    spacing: { before: 200 },
    children: [
      new TextRun({ text: config.footerLeft || "", size: 16, color: P.footerColor, font: { ascii: "Arial", eastAsia: "Microsoft YaHei" } }),
      new TextRun({ text: "                                        " }),
      new TextRun({ text: config.footerRight || "", size: 16, color: P.footerColor, font: { ascii: "Arial" } }),
    ],
  }));
  return [new Table({
    width: { size: 100, type: WidthType.PERCENTAGE },
    layout: TableLayoutType.FIXED,
    borders: allNoBorders,
    rows: [new TableRow({
      height: { value: 16838, rule: "exact" },
      children: [new TableCell({
        shading: { type: ShadingType.CLEAR, fill: P.bg }, borders: noBorders, children,
      })],
    })],
  })];
}

/* ---------------- 正文构件 ---------------- */
function h1(text) {
  return new Paragraph({
    heading: HeadingLevel.HEADING_1,
    spacing: { before: 400, after: 160, line: 380, lineRule: "atLeast" },
    children: [new TextRun({ text, bold: true, size: 32, color: P.headPrimary,
      font: { eastAsia: "SimHei", ascii: "Times New Roman" } })],
  });
}
function h2(text) {
  return new Paragraph({
    heading: HeadingLevel.HEADING_2,
    spacing: { before: 280, after: 120, line: 340, lineRule: "atLeast" },
    children: [new TextRun({ text, bold: true, size: 28, color: P.headPrimary,
      font: { eastAsia: "SimHei", ascii: "Times New Roman" } })],
  });
}
function h3(text) {
  return new Paragraph({
    heading: HeadingLevel.HEADING_3,
    spacing: { before: 220, after: 100, line: 312 },
    children: [new TextRun({ text, bold: true, size: 24, color: P.headPrimary,
      font: { eastAsia: "SimHei", ascii: "Times New Roman" } })],
  });
}
function body(text, opts = {}) {
  return new Paragraph({
    alignment: AlignmentType.JUSTIFIED,
    indent: { firstLine: 480 },
    spacing: { line: 312, after: opts.after ?? 60 },
    children: [new TextRun({ text, size: 24, color: "000000",
      font: { eastAsia: "SimSun", ascii: "Times New Roman" } })],
  });
}
function bodyRuns(runs) {
  return new Paragraph({
    alignment: AlignmentType.JUSTIFIED,
    indent: { firstLine: 480 },
    spacing: { line: 312, after: 60 },
    children: runs.map(([text, bold]) => new TextRun({ text, bold: !!bold, size: 24,
      color: "000000", font: { eastAsia: "SimSun", ascii: "Times New Roman" } })),
  });
}
function numItem(reference, text) {
  return new Paragraph({
    numbering: { reference, level: 0 },
    alignment: AlignmentType.JUSTIFIED,
    spacing: { line: 312, after: 40 },
    children: [new TextRun({ text, size: 24, color: "000000",
      font: { eastAsia: "SimSun", ascii: "Times New Roman" } })],
  });
}
function bulletItem(text) {
  return new Paragraph({
    bullet: { level: 0 },
    alignment: AlignmentType.JUSTIFIED,
    spacing: { line: 312, after: 40 },
    children: [new TextRun({ text, size: 24, color: "000000",
      font: { eastAsia: "SimSun", ascii: "Times New Roman" } })],
  });
}
function caption(text) {
  return new Paragraph({
    alignment: AlignmentType.CENTER, keepNext: true,
    spacing: { before: 200, after: 80 },
    children: [new TextRun({ text, bold: true, size: 21, color: "333333",
      font: { eastAsia: "SimSun", ascii: "Times New Roman" } })],
  });
}
function figCaption(text) {
  return new Paragraph({
    alignment: AlignmentType.CENTER,
    spacing: { before: 80, after: 200 },
    children: [new TextRun({ text, bold: true, size: 21, color: "333333",
      font: { eastAsia: "SimSun", ascii: "Times New Roman" } })],
  });
}
function cell(text, opts = {}) {
  return new TableCell({
    width: { size: opts.width, type: WidthType.PERCENTAGE },
    shading: opts.header ? { type: ShadingType.CLEAR, fill: P.table.headerBg } : undefined,
    margins: { top: 60, bottom: 60, left: 120, right: 120 },
    children: [new Paragraph({
      alignment: AlignmentType.CENTER,
      spacing: { line: 280 },
      children: [new TextRun({ text, bold: !!opts.header, size: 21,
        color: opts.header ? P.table.headerText : "000000",
        font: { eastAsia: opts.header ? "SimHei" : "SimSun", ascii: "Times New Roman" } })],
    })],
  });
}
function tbl(header, rows, widths) {
  return new Table({
    width: { size: 100, type: WidthType.PERCENTAGE },
    borders: {
      top: { style: BorderStyle.SINGLE, size: 4, color: P.table.accentLine },
      bottom: { style: BorderStyle.SINGLE, size: 4, color: P.table.accentLine },
      left: NB, right: NB,
      insideHorizontal: { style: BorderStyle.SINGLE, size: 1, color: P.table.innerLine },
      insideVertical: NB,
    },
    rows: [
      new TableRow({
        tableHeader: true, cantSplit: true,
        children: header.map((t, j) => cell(t, { header: true, width: widths[j] })),
      }),
      ...rows.map(r => new TableRow({
        cantSplit: true,
        children: r.map((t, j) => cell(t, { width: widths[j] })),
      })),
    ],
  });
}
function figure(pngPath, capText) {
  const buf = fs.readFileSync(pngPath);
  return [
    new Paragraph({
      alignment: AlignmentType.CENTER, keepNext: true, spacing: { before: 120 },
      children: [new ImageRun({ data: buf, transformation: { width: 560, height: 294 }, type: "png" })],
    }),
    figCaption(capText),
  ];
}
function pageFooter() {
  return new Footer({
    children: [new Paragraph({
      alignment: AlignmentType.CENTER,
      children: [new TextRun({ children: [PageNumber.CURRENT], size: 18, color: "808080" })],
    })],
  });
}
function pageHeader() {
  return new Header({
    children: [new Paragraph({
      alignment: AlignmentType.CENTER,
      border: { bottom: { style: BorderStyle.SINGLE, size: 2, color: "C8DDE2", space: 4 } },
      children: [new TextRun({ text: "企业数字化转型年度报告（2026）", size: 18, color: "808080",
        font: { eastAsia: "SimSun", ascii: "Times New Roman" } })],
    })],
  });
}

/* ---------------- 前置区：摘要 + 目录 ---------------- */
const frontMatter = [
  new Paragraph({
    alignment: AlignmentType.CENTER, spacing: { before: 240, after: 300 },
    children: [new TextRun({ text: "摘  要", bold: true, size: 32,
      font: { eastAsia: "SimHei", ascii: "Times New Roman" } })],
  }),
  body("2026年是示例集团数字化转型三年规划的收官之年。本报告系统回顾了全年数字化建设的实施过程、建设成果与运行效益，梳理了存在的主要问题及原因，并提出下一年度的工作安排。"),
  body("全年围绕基础设施升级、核心业务系统建设、数据平台与智能化应用三条主线开展工作：完成机房与网络改造，新版办公门户、合同管理、供应链协同等八个业务系统上线运行；数据中台完成主体建设，核心业务数据入湖比例达到百分之八十六。全年完成投资四千万元，预算执行率百分之八十八点九；线上工单量由年初的一点二万件每月增长至年末的二点三万件每月，平均处理时长由二十六点五小时下降至十四点六小时，降幅百分之四十四点九。"),
  bodyRuns([["总体判断：", true], ["年度目标基本达成，转型成效初步显现，但在数据质量、系统间协同和运营保障机制方面仍存在短板，需要在下一年度重点补齐。", false]]),
  new Paragraph({
    spacing: { before: 200 },
    children: [new TextRun({ text: "关键词：数字化转型；业务系统；数据中台；运行效益", size: 24, bold: true,
      font: { eastAsia: "SimHei", ascii: "Times New Roman" } }), new PageBreak()],
  }),
  new Paragraph({
    alignment: AlignmentType.CENTER, spacing: { before: 240, after: 360 },
    children: [new TextRun({ text: "目  录", bold: true, size: 32,
      font: { eastAsia: "SimHei", ascii: "Times New Roman" } })],
  }),
  new TableOfContents("Table of Contents", { hyperlink: true, headingStyleRange: "1-3" }),
  new Paragraph({
    spacing: { before: 200 },
    children: [new TextRun({
      text: "注：本目录由域代码生成。文档编辑后如需确保页码准确，请在目录上点击右键并选择更新域。",
      italics: true, size: 18, color: "888888",
      font: { eastAsia: "SimSun", ascii: "Times New Roman" } })],
  }),
];

/* ---------------- 正文 ---------------- */
const bodyChildren = [];

/* 一、报告概述 */
bodyChildren.push(h1("一、报告概述与编制说明"));
bodyChildren.push(h2("（一）编制目的与适用范围"));
bodyChildren.push(body("本报告旨在全面总结示例集团2026年度数字化转型项目的实施情况，为集团管理层年度经营复盘、下一年度信息化预算编制以及后续项目立项决策提供依据。报告覆盖集团总部及下属八个业务板块的数字化建设内容。"));
bodyChildren.push(body("本报告的读者对象为集团经营管理层、各业务板块分管负责人、信息化归口管理部门及项目相关参与方。涉密内容另行专项报告，不在本报告范围内。"));
bodyChildren.push(h2("（二）编制依据与数据口径"));
bodyChildren.push(body("报告主要依据以下材料编制：数字化转型三年规划及年度实施方案、项目立项批复文件、月度项目例会纪要、各信息系统运行台账、财务部投资执行台账，以及第三方测评机构出具的年度信息安全测评报告。"));
bodyChildren.push(body("除特别注明外，本报告数据统计区间为2026年1月1日至2026年10月31日；金额单位为人民币万元，均为含税口径；工单量、处理时长等运行指标取自服务台系统的月度统计报表。为保证口径一致，同比数据均按相同统计区间折算。"));

/* 二、年度目标与总体完成情况 */
bodyChildren.push(h1("二、年度目标与总体完成情况"));
bodyChildren.push(h2("（一）年度目标回顾"));
bodyChildren.push(body("年初经集团数字化转型委员会审议通过的年度目标共六项，涵盖建设进度、系统上线、数据治理、运行服务与投资管控五个方面，具体完成情况如表1所示。"));
bodyChildren.push(caption("表1　2026年度数字化转型目标完成情况"));
bodyChildren.push(tbl(
  ["序号", "年度目标", "衡量指标", "目标值", "实际完成", "完成情况"],
  [
    ["1", "完成三期工程建设", "里程碑按期完成率", "90%", "91.7%", "达成"],
    ["2", "核心系统上线运行", "新系统上线数量", "8个", "8个", "达成"],
    ["3", "核心数据入湖", "入湖比例", "80%", "86%", "超额完成"],
    ["4", "提升线上服务能力", "线上工单月均增长", "50%", "83.9%", "超额完成"],
    ["5", "缩短工单处理时长", "平均处理时长降幅", "30%", "44.9%", "超额完成"],
    ["6", "投资管控", "预算执行率区间", "85%至95%", "88.9%", "达成"],
  ],
  [8, 24, 22, 14, 16, 16],
));
bodyChildren.push(h2("（二）总体完成情况评价"));
bodyChildren.push(bodyRuns([["六项年度目标全部达成，其中三项超额完成。", true], ["从投资执行看，全年计划投资四千万元，实际完成四千万元，各季度执行情况与计划基本吻合，预算执行率百分之八十八点九，处于目标管控区间之内，如图1所示。", false]]));
bodyChildren.push(body("从建设节奏看，一季度受春节假期及机房改造停机窗口影响完成额略低于计划，二季度起施工与开发并行推进，三、四季度进入上线高峰，整体进度曲线平滑，未出现年底突击投资的情况。"));
bodyChildren.push(...figure("chart_quarterly.png", "图1　2026年分季度投资计划与完成情况（万元）"));

/* 三、实施过程回顾 */
bodyChildren.push(h1("三、重点项目实施过程回顾"));
bodyChildren.push(body("年度建设按三期滚动推进，各期之间保留一个月左右的并行观察期，用于问题收敛与经验复用。"));
bodyChildren.push(h2("（一）一期：基础设施升级阶段"));
bodyChildren.push(body("一至三月实施机房与网络改造，完成核心机房供配电与制冷系统更新、骨干网络万兆升级以及异地容灾链路建设。改造期间采用分批停机窗口切换方案，累计安排停机窗口九个，业务系统未发生超过三十分钟的非计划中断。"));
bodyChildren.push(body("同步完成虚拟化资源池扩容，计算资源供给能力较改造前提升约一点八倍，为后续业务系统集中上线预留了资源空间。"));
bodyChildren.push(h2("（二）二期：核心业务系统建设阶段"));
bodyChildren.push(body("四至八月实施核心业务系统建设，先后上线新版办公门户、合同管理、供应链协同、客户服务、人力资源、财务报账、档案管理、移动办公八个系统。建设期间完成需求确认三十二场、接口联调一百一十七项、用户验收测试四轮。"));
bodyChildren.push(body("二期采取先试点后推广策略：五月在集团总部及两个业务板块试点，六月末完成试点问题集中治理，七月至八月分三批完成其余板块推广。推广期间共组织培训二十六场，覆盖用户四千二百人次。"));
bodyChildren.push(h2("（三）三期：数据平台与智能化应用阶段"));
bodyChildren.push(body("八至十一月实施数据平台与智能化应用建设。数据中台完成主数据管理、数据集成、数据质量与基础分析四个模块的主体开发，建立客户、供应商、物料、组织、人员五类主数据标准，核心业务数据入湖比例达到百分之八十六。"));
bodyChildren.push(body("智能化应用方面，上线智能问答、工单智能分派与报表自动生成三个应用场景。工单智能分派上线后，人工分派环节平均耗时由八分钟下降至一分钟以内，分派准确率百分之九十二。"));
bodyChildren.push(h2("（四）年度里程碑达成情况"));
bodyChildren.push(body("全年共设置十二个一级里程碑，按期完成十一个，里程碑按期完成率百分之九十一点七，具体情况如表2所示。"));
bodyChildren.push(caption("表2　2026年度一级里程碑完成情况"));
bodyChildren.push(tbl(
  ["里程碑", "计划完成时间", "实际完成时间", "状态"],
  [
    ["机房改造完成", "3月20日", "3月18日", "提前"],
    ["网络升级切换", "3月31日", "3月28日", "提前"],
    ["办公门户试点上线", "5月15日", "5月15日", "按期"],
    ["八大系统推广完成", "8月31日", "8月29日", "提前"],
    ["主数据标准发布", "9月20日", "9月26日", "延期"],
    ["数据中台主体上线", "11月15日", "11月12日", "提前"],
    ["智能分派应用上线", "11月30日", "11月20日", "提前"],
  ],
  [34, 22, 22, 22],
));
bodyChildren.push(body("唯一延期的里程碑为主数据标准发布，延期六天，主要原因是供应商主数据字段在两个业务板块间存在口径分歧，经数字化转型委员会专题协调后统一。"));

/* 四、系统建设成果 */
bodyChildren.push(h1("四、系统建设成果汇总"));
bodyChildren.push(h2("（一）上线系统清单"));
bodyChildren.push(body("全年新上线及重大升级的系统共八个，全部通过用户验收测试并纳入统一运维体系，清单如表3所示。"));
bodyChildren.push(caption("表3　2026年度上线系统清单"));
bodyChildren.push(tbl(
  ["系统名称", "上线时间", "覆盖用户", "状态"],
  [
    ["新版办公门户", "5月", "全员", "稳定运行"],
    ["合同管理系统", "6月", "法务及业务经办", "稳定运行"],
    ["供应链协同平台", "7月", "采购及供应商", "稳定运行"],
    ["客户服务系统", "7月", "客服中心", "稳定运行"],
    ["人力资源系统", "8月", "全员", "稳定运行"],
    ["财务报账系统", "8月", "全员", "稳定运行"],
    ["档案管理系统", "8月", "档案及综合岗", "稳定运行"],
    ["移动办公平台", "8月", "全员", "稳定运行"],
  ],
  [34, 18, 26, 22],
));
bodyChildren.push(h2("（二）系统集成与数据贯通情况"));
bodyChildren.push(body("全年新建系统间接口一百一十七项，统一身份认证平台覆盖全部新上线系统，实现单点登录。组织人员、客户、供应商三类主数据由主数据管理模块统一分发，改变了此前各系统分别维护、口径不一的局面。"));
bodyChildren.push(body("财务报账系统与合同管理系统实现合同付款节点联动，付款申请自动校验合同余额与付款条件，上线以来拦截不合规付款申请四十三笔。"));
bodyChildren.push(h2("（三）用户覆盖与培训情况"));
bodyChildren.push(body("截至十月底，八个系统累计开通账号四千六百个，月度活跃账号三千九百个，活跃率百分之八十四点八。全年组织集中培训二十六场、在线课程十九门，覆盖四千二百人次，人均培训时长四点一小时。"));

/* 五、运行数据与效益分析 */
bodyChildren.push(h1("五、运行数据与效益分析"));
bodyChildren.push(h2("（一）主要运行指标"));
bodyChildren.push(body("线上工单量与平均处理时长是反映数字化服务能力的核心指标。全年线上工单量由一月的一点二四万件增长至十月的二点二八万件，月均复合增长百分之五点七；平均处理时长由二十六点五小时下降至十四点六小时，如图2所示。"));
bodyChildren.push(...figure("chart_monthly.png", "图2　2026年月度线上工单量与平均处理时长"));
bodyChildren.push(body("工单量上升而处理时长下降，说明业务流程线上化迁移与服务资源投入形成了正向组合效应：常见问题由知识库与智能问答承接，复杂问题经智能分派直达责任岗位，减少了以往层层转派造成的等待。"));
bodyChildren.push(h2("（二）效益指标汇总"));
bodyChildren.push(body("年度效益从效率、质量、成本三个维度衡量，主要指标如表4所示。"));
bodyChildren.push(caption("表4　2026年度主要效益指标"));
bodyChildren.push(tbl(
  ["维度", "指标", "年初值", "年末值", "变化幅度"],
  [
    ["效率", "工单平均处理时长", "26.5小时", "14.6小时", "下降44.9%"],
    ["效率", "合同审批周期", "6.8个工作日", "3.2个工作日", "下降52.9%"],
    ["效率", "月度结账周期", "5.5个工作日", "3.0个工作日", "下降45.5%"],
    ["质量", "数据一致性问题月均数", "86项", "31项", "下降64.0%"],
    ["成本", "纸质档案归档量", "12.6万页", "3.4万页", "下降73.0%"],
  ],
  [12, 34, 18, 18, 18],
));
bodyChildren.push(h2("（三）投资执行情况"));
bodyChildren.push(body("年度预算四千五百万元，实际完成投资四千万元，预算执行率百分之八十八点九。结余主要来自网络设备集中采购降价与二期推广阶段差旅费用压缩。按投资构成划分：基础设施建设百分之三十一，软件购置与开发百分之四十四，实施服务百分之十七，培训与推广百分之八。"));

/* 六、问题诊断与原因分析 */
bodyChildren.push(h1("六、问题诊断与原因分析"));
bodyChildren.push(h2("（一）主要问题清单"));
bodyChildren.push(body("按影响程度排序，年度建设与运行中暴露的主要问题共五项，如表5所示。"));
bodyChildren.push(caption("表5　年度主要问题清单"));
bodyChildren.push(tbl(
  ["编号", "问题", "影响范围", "严重程度"],
  [
    ["P1", "部分历史数据质量不高，清洗返工量大", "数据中台", "高"],
    ["P2", "系统间流程衔接存在断点，仍需线下沟通", "供应链、客服", "高"],
    ["P3", "运维人员对新系统掌握不足，故障定位偏慢", "全部新系统", "中"],
    ["P4", "移动端部分功能适配不佳，老年用户反馈较多", "移动办公", "中"],
    ["P5", "需求变更管理偏松，二期出现范围蔓延", "在建项目", "中"],
  ],
  [10, 40, 28, 22],
));
bodyChildren.push(h2("（二）原因分析"));
bodyChildren.push(h3("1. 数据层面：源头治理机制缺位"));
bodyChildren.push(body("历史系统长期缺乏数据标准与校验规则，质量责任未落实到源头业务部门，数据中台建设阶段集中暴露，清洗与返工消耗了约百分之十五的项目工时。"));
bodyChildren.push(h3("2. 流程层面：跨部门端到端视角不足"));
bodyChildren.push(body("各系统建设以部门需求为边界立项，跨部门流程在系统间缺少统一的流程责任人，导致订单到收款、采购到付款等端到端链条在系统边界处仍存在线下衔接环节。"));
bodyChildren.push(h3("3. 组织层面：运维体系与建设节奏不匹配"));
bodyChildren.push(body("项目团队以建设交付为导向，运维知识转移在上线高峰期被压缩，一线运维人员独立处置率仅百分之六十七，低于百分之八十的年度目标。"));

/* 七、经验教训与改进措施 */
bodyChildren.push(h1("七、经验教训与改进措施"));
bodyChildren.push(h2("（一）主要经验"));
bodyChildren.push(body("回顾全年，以下做法被实践证明有效，建议在后续项目中延续："));
bodyChildren.push(bulletItem("先试点后推广：二期系统采取试点集中治理再分批推广的策略，推广阶段问题量较试点阶段下降约六成。"));
bodyChildren.push(bulletItem("停机窗口集中切换：机房改造期间统一规划停机窗口，避免了改造对业务的反复扰动。"));
bodyChildren.push(bulletItem("主数据先行：五类主数据标准先于数据中台功能建设发布，为入湖工作扫清了口径障碍。"));
bodyChildren.push(bulletItem("培训与上线绑定：将用户培训完成率作为系统上线的前置条件，保障了推广期的使用熟练度。"));
bodyChildren.push(h2("（二）教训与反思"));
bodyChildren.push(body("一是对历史数据质量的严重性估计不足，项目前期未安排专项数据治理资源；二是需求变更管理执行偏松，二期累计受理变更需求五十四项，其中约两成属于范围外新增；三是运维知识转移未作为硬性交付物管理，导致上线后服务响应承压。"));
bodyChildren.push(h2("（三）改进措施"));
bodyChildren.push(body("针对第六章诊断的问题，制定以下改进措施，并明确责任部门与完成时限："));
bodyChildren.push(numItem("list-improve", "建立数据质量责任制，由数字化办公室牵头，各业务部门指定数据专员，2027年一季度内完成制度发布与考核挂钩。"));
bodyChildren.push(numItem("list-improve", "梳理订单到收款、采购到付款等五条端到端流程，指定流程责任人，2027年上半年完成断点改造。"));
bodyChildren.push(numItem("list-improve", "将运维知识转移纳入项目验收硬性条件，编制标准化运维手册并组织上岗认证，2027年二季度起执行。"));
bodyChildren.push(numItem("list-improve", "开展移动端适老化改造专项，优化字体、按钮与操作路径，2027年一季度完成并组织老年用户测评。"));
bodyChildren.push(numItem("list-improve", "修订需求变更管理办法，变更评审引入工时影响评估，范围外需求一律走立项程序。"));

/* 八、下一年度计划 */
bodyChildren.push(h1("八、下一年度工作计划与保障措施"));
bodyChildren.push(h2("（一）重点工作安排"));
bodyChildren.push(body("2027年度围绕深化应用、数据增值与智能扩展三条主线安排七项重点工作，如表6所示。"));
bodyChildren.push(caption("表6　2027年度重点工作安排"));
bodyChildren.push(tbl(
  ["重点工作", "主要产出", "完成时间"],
  [
    ["端到端流程断点改造", "五条流程全线上贯通", "6月"],
    ["数据质量治理专项", "质量问题月均数再降五成", "二季度起持续"],
    ["数据中台分析能力建设", "经营驾驶舱上线", "9月"],
    ["智能应用扩展", "新增三个智能场景", "11月"],
    ["移动端适老化改造", "完成并组织测评", "3月"],
    ["信息安全等级保护复测", "通过复测并完成整改", "10月"],
    ["运维体系认证", "运维人员上岗认证全覆盖", "二季度起"],
  ],
  [36, 40, 24],
));
bodyChildren.push(h2("（二）预算安排"));
bodyChildren.push(body("2027年度信息化预算建议四千二百万元，重点向数据治理与分析能力建设倾斜，安排如表7所示。"));
bodyChildren.push(caption("表7　2027年度预算安排建议"));
bodyChildren.push(tbl(
  ["支出方向", "金额（万元）", "占比", "较2026年变化"],
  [
    ["数据治理与分析能力建设", "1470", "35.0%", "提升12个百分点"],
    ["智能应用扩展", "840", "20.0%", "提升6个百分点"],
    ["系统运维与优化", "966", "23.0%", "提升7个百分点"],
    ["基础设施续保与安全", "588", "14.0%", "下降18个百分点"],
    ["培训与推广", "336", "8.0%", "持平"],
  ],
  [34, 20, 16, 30],
));
bodyChildren.push(h2("（三）组织与机制保障"));
bodyChildren.push(body("继续依托集团数字化转型委员会统筹重大事项决策，实行月度例会与季度向经营管理层汇报机制。项目层面推行双负责人制，业务部门负责人对需求与推广效果负责，数字化办公室项目负责人对方案与交付负责。"));
bodyChildren.push(body("同时将数字化转型指标纳入业务板块年度绩效考核，权重百分之五，考核维度包括系统使用率、数据质量达标率与流程线上化率。"));

/* 附录A */
bodyChildren.push(h1("附录A　术语与缩略语"));
bodyChildren.push(body("本报告使用的主要术语与缩略语如表8所示。"));
bodyChildren.push(caption("表8　术语与缩略语对照表"));
bodyChildren.push(tbl(
  ["术语或缩略语", "全称或释义"],
  [
    ["数据中台", "承担数据汇聚、治理与服务共享的企业级数据能力平台"],
    ["主数据", "描述核心业务对象、跨系统共享引用的基础数据"],
    ["入湖", "将业务数据汇聚至数据平台统一存储的过程"],
    ["工单", "通过线上服务台受理的服务请求或问题报告"],
    ["单点登录", "一次认证即可访问多个授权系统的登录机制"],
    ["端到端流程", "跨部门、跨系统完整贯通的业务流程"],
    ["预算执行率", "实际完成投资与年度预算的比值"],
    ["里程碑", "项目计划中具有标志意义的关键节点"],
  ],
  [30, 70],
));

/* 附录B */
bodyChildren.push(h1("附录B　项目里程碑明细"));
bodyChildren.push(body("全年十二个一级里程碑的明细信息如表9所示，其中带星号者为延期完成节点。"));
bodyChildren.push(caption("表9　2026年度一级里程碑明细"));
bodyChildren.push(tbl(
  ["序号", "里程碑名称", "计划日期", "实际日期", "偏差", "所属阶段"],
  [
    ["1", "机房供配电改造完成", "2月28日", "2月25日", "提前3天", "一期"],
    ["2", "骨干网络万兆切换", "3月31日", "3月28日", "提前3天", "一期"],
    ["3", "容灾链路建设验收", "3月20日", "3月18日", "提前2天", "一期"],
    ["4", "办公门户试点上线", "5月15日", "5月15日", "按期", "二期"],
    ["5", "合同管理系统上线", "6月20日", "6月18日", "提前2天", "二期"],
    ["6", "供应链协同平台上线", "7月25日", "7月24日", "提前1天", "二期"],
    ["7", "八大系统推广完成", "8月31日", "8月29日", "提前2天", "二期"],
    ["8", "主数据标准发布", "9月20日", "9月26日", "延期6天*", "三期"],
    ["9", "数据集成模块上线", "10月15日", "10月12日", "提前3天", "三期"],
    ["10", "数据质量模块上线", "10月31日", "10月28日", "提前3天", "三期"],
    ["11", "数据中台主体上线", "11月15日", "11月12日", "提前3天", "三期"],
    ["12", "智能分派应用上线", "11月30日", "11月20日", "提前10天", "三期"],
  ],
  [10, 30, 16, 16, 14, 14],
));
bodyChildren.push(body("以上明细与第三章表2的汇总口径一致。年度结束前剩余的一个半月为并行观察与问题收敛期，不设置新增里程碑。", { after: 200 }));

/* ---------------- 组装文档 ---------------- */
const pgSize = { width: 11906, height: 16838 };
const pgMargin = { top: 1440, bottom: 1440, left: 1701, right: 1417 };

const doc = new Document({
  creator: "示例集团数字化转型办公室",
  title: "企业数字化转型年度报告（2026）",
  styles: {
    default: {
      document: {
        run: { font: { ascii: "Times New Roman", eastAsia: "SimSun" }, size: 24, color: "000000" },
        paragraph: { spacing: { line: 312 } },
      },
      heading1: {
        run: { font: { ascii: "Times New Roman", eastAsia: "SimHei" }, size: 32, bold: true, color: P.headPrimary },
        paragraph: { spacing: { before: 400, after: 160 }, outlineLevel: 0 },
      },
      heading2: {
        run: { font: { ascii: "Times New Roman", eastAsia: "SimHei" }, size: 28, bold: true, color: P.headPrimary },
        paragraph: { spacing: { before: 280, after: 120 }, outlineLevel: 1 },
      },
      heading3: {
        run: { font: { ascii: "Times New Roman", eastAsia: "SimHei" }, size: 24, bold: true, color: P.headPrimary },
        paragraph: { spacing: { before: 220, after: 100 }, outlineLevel: 2 },
      },
    },
  },
  numbering: {
    config: [{
      reference: "list-improve",
      levels: [{
        level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT,
        style: { paragraph: { indent: { left: 720, hanging: 360 } } },
      }],
    }],
  },
  sections: [
    { /* 第1节：封面（无页码、无页眉页脚） */
      properties: { page: { size: pgSize, margin: { top: 0, bottom: 0, left: 0, right: 0 } } },
      children: buildCoverR1({
        title: "企业数字化转型年度报告",
        subtitle: "示例集团 2026 年度建设与运行总结",
        englishLabel: "ANNUAL REPORT 2026",
        metaLines: [
          "编制单位：示例集团数字化转型办公室",
          "报告周期：2026年1月至2026年12月",
          "密级：内部资料",
          "编制日期：2026年11月",
        ],
        footerLeft: "示例集团数字化转型办公室",
        footerRight: "版本 V1.0",
      }),
    },
    { /* 第2节：前置区（摘要+目录，罗马页码） */
      properties: {
        type: SectionType.NEXT_PAGE,
        page: { size: pgSize, margin: pgMargin,
                pageNumbers: { start: 1, formatType: NumberFormat.UPPER_ROMAN } },
      },
      headers: { default: pageHeader() },
      footers: { default: pageFooter() },
      children: frontMatter,
    },
    { /* 第3节：正文（阿拉伯页码，从1起） */
      properties: {
        type: SectionType.NEXT_PAGE,
        page: { size: pgSize, margin: pgMargin,
                pageNumbers: { start: 1, formatType: NumberFormat.DECIMAL } },
      },
      headers: { default: pageHeader() },
      footers: { default: pageFooter() },
      children: bodyChildren,
    },
  ],
});

const OUT = "C:/Users/Wren-GS/Desktop/企业数字化转型年度报告_测试样例.docx";
Packer.toBuffer(doc).then(buf => {
  fs.writeFileSync(OUT, buf);
  console.log("written:", OUT, buf.length, "bytes");
});
