# Auto-ORCA 中文研究报告

当前报告：**Auto-ORCA: LLM-Guided Autonomous Exploration of Potential Energy Surfaces Using DFT**。

本目录的 `main.tex` 为本次修改的主文件，原位置已覆盖。报告参考用户提供论文的章节组织，扩充至约 15 页，采用“引言—相关工作与研究动机—科学任务的理论基础—Agent 架构—计算方法与 Agent 能力测试—测试结果—讨论—结论”。七题之前明确说明测试目的，并给出能力与证据对照表。正文以当前七个问题的结果替换旧案例和统计；各题按“问题、方法、结果”展开。T1–T5 与 H1 成功，H2 未完成，以 ORCA 正常完成且得到数值为口径，计算成功率为 6/7（85.7%）。T3 仍明确披露实际角度未确认及 +180° 未计算，其他科学解释限制保留。

- `main.tex`：主文及章节组织。
- `v4-calculation-results.tex`：当前七题的数值、完整频率与屏蔽张量；文件名沿用原有依赖。
- `references.tex`：正文实际引用的 24 项文献。
- `figures/auto-orca-architecture.png` 与 `figures/agent-capability-map.png`：本次生成的原创系统架构与七题能力测试示意图。
- `image-prompts-current.txt`：两张图的完整提示词与内置生图模式记录；工具未回传具体模型身份。
- `final-report-zh-academic.pdf`：与当前源码一致的编译阅读版。
- `successful-calculations.json`：当前数值、结果口径和原始来源校验。
- `make_current_results_figure.py`：当前正己烷与丙酮数值图的绘图程序。
- `validation.json`：当前编译、数值与排版验证记录。

在本目录用 XeLaTeX 编译两次 `main.tex` 即可；参考文献直接内嵌，无需 BibTeX。两张原创示意图与基于真实结果绘制的数值图随源码提供。

其余历史数据、图表与阅读版为原有资料，不被当前主文引用，也不用于当前七题的成功率。实际运行记录未改写。本次扩写文档并生成示意图，未新增 ORCA 计算或 Agent 能力测试。
