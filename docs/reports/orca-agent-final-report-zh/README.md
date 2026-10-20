# 中文 Final Report

论文式中文报告，以摘要开始，无独立封面。正文包括引言、相关工作、科学背景、系统方法、实验设计、结果、讨论、后续验证、结论、参考文献和证据附录。

- `main.tex`：主文档，UTF-8，`ctexart`，XeLaTeX。
- `references.tex`：30 篇学术文献与 1 项 OPI 官方文档，全部在正文引用；数字编号按首次引用排序。
- `final-report-zh.pdf`：经过编译和页面检查的阅读版。
- `figures/method-overview.png`：GPT Image 辅助生成的方法示意，仅表示概念关系。
- `figures/` 其余三组 PDF/PNG：直接从实验数据绘制的量化图。
- `experiment-data.json`、`make_figures.py`：图表数据与复算脚本。
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

日期：2026-10-21，Asia/Hong_Kong。历史实验 Git 截点为 `def9e31d`，最新正式实验的产品候选为 `89d55708`，不是报告提交后的新 Git HEAD。当前 v78/0057 的未提交开发修复只列为进展，不能当作新真实自主成功证据。

早期乙醇自主正例、包含开发者元数据协助的有限构象案例、开发者底座探针与正式七题评测分别报告。最新正式七题完整目标为 0/7；正常结束的计算不等于科学资格或完整交付。四批候选不同，历史 28 次任务尝试不能作为同一版本重复成功概率。

本次仅撰写报告、核查文献和制作图表，未启动新的产品真实模型或 ORCA 测试；GPT Image 制图属于报告制作。尚无新候选真实复测、完整重复/参考/控制矩阵或真实集群结论。交付状态：等待用户验收。
