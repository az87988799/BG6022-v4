# 中文 Final Report：独立 LaTeX 工程

**在 VS Code 中打开整个 `FinalReport-LaTeX` 文件夹。** 本文件夹可整体复制到其他位置；编译不依赖 BG6022 源码、Python、ORCA 或其他目录。

## 开始编译

1. 安装 VS Code 扩展 **LaTeX Workshop**（打开文件夹时会推荐）。当前系统已有 TeX Live。
2. 打开 `main.tex`，按 **Ctrl+Alt+B**，选择 `XeLaTeX (two passes)`。
3. 按 **Ctrl+Alt+V** 打开 PDF；输出为 `build/main.pdf`，支持源文件与 PDF 跳转。

也可以在 VS Code 的 PowerShell 终端执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1
```

脚本优先使用 PATH 中的 XeLaTeX，并查找常见 TeX Live 安装目录。如果复制到其他电脑，请安装包含中文支持的 TeX Live，并将其 bin 目录加入 PATH。中文使用 TeX Live 自带 Fandol 字体，无需安装额外系统字体。

## 在哪里修改

| 文件 | 内容 |
| --- | --- |
| `main.tex` | 标题、页边距、字号、字体、章节顺序 |
| `sections/00-abstract.tex` | 摘要 |
| `sections/01-introduction.tex` | 引言 |
| `sections/02-related-work.tex` | 相关工作、计算成功的定义 |
| `sections/03-v2.tex` | V2 思路与结果 |
| `sections/04-v3.tex` | V3 思路与结果 |
| `sections/05-v4-method.tex` | V4 方法与系统设计 |
| `sections/06-v4-results.tex` | V4 实验设置、历史案例与冻结批次 |
| `sections/06a-calculation-results.tex` | **T1–T5、H1 的数值表与张量，H2 的结果状态** |
| `sections/07-discussion.tex` | 讨论 |
| `sections/08-conclusion.tex` | 结论与数据可用性 |
| `references.tex` | 参考文献；正文用 `\cite{文献键}` 引用 |
| `figures/` | 编译所需图片，已包含全部文件 |
| `data/` | 可编辑 CSV 及有来源的结构化结果 |
| `geometries/ethanol-a010.xyz` | T1 优化后结构 |

参考文献采用 `thebibliography`，不需要 BibTeX/Biber。每个章节首行已指定主文件，可直接从章节文件发起编译。新加章节时在 `main.tex` 中加入 `\input{sections/文件名.tex}`。

表格数值直接保存在 TeX，方便编辑。`data/` 便于核查、另作图表，修改 CSV 不会自动改写正文；如修改实验数据，请同步修改对应 TeX 表。

## 本次内容更新

以 **ORCA 正常结束** 判定“计算成功”；计算结果、科学解释范围、工作流交付分别说明。新增 T1 a010、T2 a005/a007、T3 历史六点及较早点、T4 五态激发、T5 a026/a027、H1 a011 的实际结果。H2 尚无 ORCA 极化率数值。不同批次与方法分别列示，六类结果覆盖不作为同一版本的 6/7 成功率。

已有完整检查后的阅读版为 `report.pdf`；继续修改后的最新输出以 `build/main.pdf` 为准。编译报错时查看 `build/main.log`；不要改用 pdfLaTeX。
