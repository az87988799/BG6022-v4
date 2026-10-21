# 中文学术报告：V2 → V3 → V4

## 当前可编辑版本（2026-10-21 更新）

**请在 VS Code 中打开仓库根目录的 [`FinalReport-LaTeX`](../../../FinalReport-LaTeX/README.md) 文件夹。** 该独立工程已按章节拆分，包含编译所需全部图片、参考文献、VS Code 配置及 `build.ps1`；无需其他仓库文件或 Python/ORCA。主文件为 `FinalReport-LaTeX/main.tex`，按 Ctrl+Alt+B 编译，输出 `build/main.pdf`。已编译阅读版为其中的 `report.pdf`。

本次按用户要求，以 ORCA 正常结束标为“计算成功”，补入 T1 a010、T2 a005/a007、T3 历史六点及较早计算点、T4 五态激发、T5 a026/a027、H1 a011 的数值表与完整张量。H2 明确为尚无极化率结果。原冻结批次 7/8（87.5%）为已启动调用的计算成功比例，后续六类任务结果来自不同批次，不混作同版本成功率；历史完整工作流状态保持原样。本次只读取已有记录，没有新增科学或模型计算。

`successful-calculations.json` 保存本次新增数值与九份来源的内容校验信息。`v4-calculation-results.tex` 是对应表格内容。下方记录此前学术版的组织与来源；旧 PDF 与旧源码 ZIP 不代表本次最新内容。

当前主文按三个研究思路的发展顺序组织，重点为 V4 的方法与实验评估。

1. 引言
2. 相关工作与评价基础
3. V2：以过程一致性为中心的可追溯工作流
4. V3：有限任务范围内的语义驱动工具组合
5. V4：证据约束的代码生成与单循环决策
6. V4 的实验设计与结果
7. 讨论
8. 结论

正文采用学术表述，不再包含机器路径、操作命令、提交记录、验收状态或开发流水。软件与方法版本、资源约束、人工参与、评价分母及科学局限作为实验条件保留。V4 方法、实验与针对性讨论为主体；V2/V3 用于建立研究问题的演进。

## 当前交付

- `main.tex`：主文件，UTF-8、ctexart、XeLaTeX。
- `historical-results.tex`：按顺序展开 V2 与 V3 的思路、成果和边界。
- `references.tex`：31 篇学术文献及 1 项官方文档，按首次引用排序。
- `final-report-zh-academic.pdf`：本次按演进顺序重写的学术阅读版。
- `figures/`：1 幅概念示意图、6 幅量化图；量化图已统一学术图题，去除提交标识，数据不变。
- 三份实验 JSON 与三份绘图脚本：原有 V2/V3/V4 数值及复算方法。
- `source-manifest.json`、`sources-bibliography.json`：实验与文献来源，作为补充材料保存。
- `validation.json`：数据、引用、编译、排版与结构检查结果。
- `image-generation-prompt.txt`：概念示意图的生成说明。

普通编译在本目录执行两次 `xelatex main.tex`。参考文献直接内嵌，无需 BibTeX/Biber；量化图 PDF 已提供，编译不需要 Python。图表复算依赖和命令见 `figures/README.md` 与 `requirements-figures.txt`。

## 数据与解释范围

此前的学术叙事修订没有新增模型/科学计算。V2 两条人工批准的计算链、V3 固定矩阵和预定义对话情境、V4 个案与七任务评测分开评价，不据此作跨版本性能排名。本次在保留原冻结批次数据的基础上，显式补入用户指定的后续成功计算，按批次分别报告。

`final-report-zh.pdf` 与 `final-report-zh-expanded.pdf` 为此前阅读版，保留供版本对照，不进入本次学术源码包。当前以 academic 文件为准。技术来源路径保留在补充 JSON 中，不进入论文正文。
