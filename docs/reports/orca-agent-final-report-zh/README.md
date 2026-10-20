# 中文 Final Report

论文式中文报告，以摘要开始，无独立封面。正文包括引言、相关工作、科学背景、系统方法、实验设计、结果、讨论、后续验证、结论、参考文献和证据附录。

- `main.tex`：主文档，UTF-8，`ctexart`，XeLaTeX。
- `references.tex`：31 篇学术文献与 1 项 OPI 官方文档，全部在正文引用；数字编号按首次引用排序。
- `final-report-zh-expanded.pdf`：包含 v2/v3 成果、经过编译和页面检查的 20 页扩充阅读版。
- `figures/method-overview.png`：GPT Image 辅助生成的方法示意，仅表示概念关系。
- `figures/` 其余六组 PDF/PNG：直接从实验数据绘制的量化图。
- `experiment-data.json`、`make_figures.py`：图表数据与复算脚本。
- `historical-results.tex`：v2/v3 的科学成果、基准和跨版本解读。
- `historical-v2-evidence.json`、`historical-v3-evidence.json`：历史量化数据、原始路径/hash/行号及解释限制。
- `make_historical_figures.py`、`make_v3_figures.py`：v2 收敛曲线与 v3 科学矩阵/核心成本图。
- `source-manifest.json`：本地证据文件路径、读取快照 hash 与 Git 边界。
- `sources-bibliography.json`：文献核查标识与一手来源 URL。
- `image-generation-prompt.txt`：内置生图工具所用完整提示词和原始文件位置。
- `validation.json`：引用、数字、图片依赖、编译和页面检查结果。

## 编译

在本目录运行两次：

```powershell
xelatex -interaction=nonstopmode -halt-on-error main.tex
xelatex -interaction=nonstopmode -halt-on-error main.tex
```

参考文献直接使用 `thebibliography`，无需 BibTeX/Biber；Fandol 字体随常规中文 TeX 发行提供。图表 PDF 和 PNG 均已包含，普通编译无需 Python 或网络。

图表重新生成见 `figures/README.md`。绘图脚本只分析已归档数据，不调用模型、ORCA 或容器。量化图优先使用矢量 PDF；生图示意不承载实验数据。

## 报告与实验边界

日期：2026-10-21，Asia/Hong_Kong。本次依用户要求补充 v2/v3 历史可量化成果。v4 实验仍采用原稿检索截点 `def9e31d` 所选的 `refreeze-3`（产品 `89d55708`），不随报告提交重新计量。v78/0057 修复已另行提交为 `2e872a18`，本次不重新评价后续候选。

v2 两条人工批准的 Opt/Freq/SP 成功链共 6 个节点，水/乙醇为 4/11 个优化周期、3/21 个正振动模式，两个归档各 33 个文件 hash/大小匹配。存在开发者采集修复，不估计自主成功率。

v3 固定科学矩阵 11/11、16 次真实 ORCA、0 LLM；Benchmark 1.2 core 最终评分 18/18，含 37 次真实模型调用、147,938 tokens、23 次 ORCA 尝试和 1 次预设受控失败。核心分母包含澄清与拒绝，固定话语/自动确认、评分校准、未保存 dirty-tree hash 与未运行 full/holdout 均在正文披露。它与 v4 的 0/7 不是共同基准的优劣比较。

早期乙醇自主正例、包含开发者元数据协助的有限构象案例、开发者底座探针与正式七题评测分别报告。最新正式七题完整目标为 0/7；正常结束的计算不等于科学资格或完整交付。四批候选不同，历史 28 次任务尝试不能作为同一版本重复成功概率。

本次仅补充历史报告与图表，未修改旧仓库、产品代码/提示/配置，未启动新模型或 ORCA 测试；原稿 GPT Image 制图属于报告制作。未纳入后续新候选复测或真实集群结论。历史数值重复性不表示相对外部高精度参考的准确度。交付状态：等待用户验收。

原有 `final-report-zh.pdf` 为仓库中保留的 16 页初稿；扩充源码包只收录新版 PDF，当前阅读以 expanded 文件为准。
