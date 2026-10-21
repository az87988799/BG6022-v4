# Auto-ORCA 中文研究报告

当前报告：**Auto-ORCA: LLM-Guided Autonomous Exploration of Potential Energy Surfaces Using DFT**。

本目录的 `main.tex` 为本次修改的主文件，原位置已覆盖。报告现为 20 页，采用“引言—相关工作与研究动机—科学任务的理论基础—Agent 架构—从自然语言到计算报告的端到端流程—计算方法与 Agent 能力测试—测试结果—讨论—结论”。参考 AutoDFT（arXiv:2605.26179v2）的闭环问题组织方式，补充开环与单 Agent 闭环的原创机制对照图及比较表；此对照不代表新增基线实验。流程章节逐步解释意图识别、条件确认、目标固定、依赖规划、代码生成、执行归档、检查反馈与最终输出，并以乙醇及配对环境计算说明信息传递。

七题之前明确说明 Agent 能力测试目的，并给出能力与证据对照表。各题按“问题、方法、结果”展开。T1–T5 与 H1 成功，H2 未完成，以 ORCA 正常完成且得到数值为口径，计算成功率为 6/7（85.7%）。T3 仍明确披露实际角度未确认及 +180° 未计算，其他科学解释限制保留。正己烷与丙酮的两幅数值图已从正文移除，对应结果表保留。

- `main.tex`：主文及章节组织。
- `v4-calculation-results.tex`：当前七题的数值、完整频率与屏蔽张量；文件名沿用原有依赖。
- `references.tex`：正文实际引用的 24 项文献。
- `figures/auto-orca-architecture.png` 与 `figures/agent-capability-map.png`：系统架构与七题能力测试示意图。
- `figures/open-loop-vs-auto-orca.png`：新增的原创开环／闭环工作机制对照图。
- `image-prompts-current.txt`：两张图的完整提示词与内置生图模式记录；工具未回传具体模型身份。
- `image-prompts-workflow.txt`：新对照图的完整提示词、参考来源及生成模式记录。
- `final-report-zh-academic.pdf`：与当前源码一致的编译阅读版。
- `successful-calculations.json`：当前数值、结果口径和原始来源校验。
- `token-cost-audit.json`：六次已有 ORCA 产出对应的完整 Agent token 用量、登记费率估算及原始回执校验；另列一项用量不完整的记录。
- `make_current_results_figure.py`：已从正文移除的数值图绘图程序，保留供复用。
- `validation.json`：当前编译、数值与排版验证记录。

在本目录用 XeLaTeX 编译两次 `main.tex` 即可；参考文献直接内嵌，无需 BibTeX。正文使用的三张原创示意图随源码提供。

主文、测试结果和参考文献源码已按句子与短语分行，表格和公式按环境缩进，便于在 VS Code 中直接编辑。空白行表示段落；行末 `%` 用来保持源码换行前后的排版一致。仅源码格式整理时，19 页 PDF 的逐页像素与整理前完全一致。

正文新增 token 用量与费用表：五个 `deepseek-flash` 运行按登记的输入 0.30 USD／百万 tokens、输出 1.20 USD／百万 tokens 折算；未计缓存优惠，属于保守估算而非实付账单。`deepseek-v4-pro` 未登记适用单价，保留 token 数、费用记为未知。统计范围是对应 Agent Run 的全部模型调用，不是 ORCA 程序本身或单个扫描点的 token。

其余历史数据、图表与阅读版为原有资料，不被当前主文引用，也不用于当前七题的成功率。实际运行记录未改写。本次扩写文档并生成示意图，未新增 ORCA 计算或 Agent 能力测试。
