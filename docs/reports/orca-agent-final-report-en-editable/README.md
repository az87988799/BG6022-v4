# Complete Editable English TeX Manuscript

Edit **main.tex**. It contains the entire current concise English manuscript: preamble, title, CHEN GENG's author line, abstract, all sections, tables, equations, and the complete bibliography. No additional `.tex` or `.bib` files are required. This version changes only the source-file organization, not the manuscript's content, data, or structure.

Keep the accompanying `figures/` directory next to `main.tex`; it contains the three original diagrams. The text is consolidated into one TeX file, but the three PNG images are still required. Copying only `main.tex` to another directory will leave those images missing.

For VS Code, extract **all** contents of `auto-orca-editable.zip` first, then open `auto-orca-editable.code-workspace`. Alternatively, use **File > Open Folder** and select this directory. The included `.vscode/settings.json` applies only to this manuscript folder and configures LaTeX Workshop to run XeLaTeX through latexmk, which repeats compilation as needed to resolve citations and cross-references. It uses TeX Live commands from PATH, without a machine-specific installation path.

Open `main.tex`, press **Ctrl+Alt+B** to build, and wait for successful completion. Then press **Ctrl+Alt+V** to view the generated `main.pdf` beside `main.tex`. The View command does not compile a document; an initial build is required. TeX Live (including XeLaTeX and latexmk) and the LaTeX Workshop extension must be installed.

For command-line use, compile from this directory using **XeLaTeX**, twice:

```powershell
xelatex -interaction=nonstopmode -halt-on-error main.tex
xelatex -interaction=nonstopmode -halt-on-error main.tex
```

For an online LaTeX editor, upload the ZIP contents, choose `main.tex` as the main document, and select XeLaTeX. No shell escape or bibliography-processing step is required.

The earlier modular source remains in `../orca-agent-final-report-en/`. This consolidated copy is intended for direct editing; later changes to one copy do not automatically synchronize with the other.

Status: awaiting user review and acceptance.
