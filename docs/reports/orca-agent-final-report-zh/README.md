# Auto-ORCA 中文研究报告

当前报告：**Auto-ORCA: LLM-Guided Autonomous Exploration of Potential Energy Surfaces Using DFT**。

本目录的 `main.tex` 为本次修改的主文件，原位置已覆盖。报告参考用户提供论文的章节组织，采用“引言—Agent 架构—计算方法与评价—结果与讨论—结论”。正文以当前七个问题的结果替换旧案例和统计；各题按“问题、方法、结果”展开。T1–T5 与 H1 成功，H2 未完成，以 ORCA 正常完成且得到数值为口径，计算成功率为 6/7（85.7%）。T3 仍明确披露实际角度未确认及 +180° 未计算，其他科学解释限制保留。

- `main.tex`：主文及章节组织。
- `v4-calculation-results.tex`：当前七题的数值、完整频率与屏蔽张量；文件名沿用原有依赖。
- `references.tex`：正文实际引用的文献。
- `final-report-zh-academic.pdf`：与当前源码一致的编译阅读版。
- `successful-calculations.json`：当前数值、结果口径和原始来源校验。
- `make_current_results_figure.py`：当前正己烷与丙酮数值图的绘图程序。
- `validation.json`：当前编译、数值与排版验证记录。

在本目录用 XeLaTeX 编译两次 `main.tex` 即可；参考文献直接内嵌，无需 BibTeX。已有架构图与当前数值图随源码提供。

其余历史数据、图表与阅读版为原有资料，不被当前主文引用，也不用于当前七题的成功率。实际运行记录未改写。本次仅修订文档与图表，未启动新的 ORCA 或模型计算。
