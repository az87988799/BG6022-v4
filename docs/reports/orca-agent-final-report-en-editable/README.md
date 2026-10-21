# Complete Editable English TeX Manuscript

Edit **main.tex**. It contains the entire current concise English manuscript: preamble, title, CHEN GENG's author line, abstract, all sections, tables, equations, and the complete bibliography. No additional `.tex` or `.bib` files are required. This version changes only the source-file organization, not the manuscript's content, data, or structure.

Keep the accompanying `figures/` directory next to `main.tex`; it contains the three original diagrams. For portable editing, extract `auto-orca-editable.zip` and open its `main.tex`.

Compile from this directory using **XeLaTeX**, twice:

```powershell
xelatex -interaction=nonstopmode -halt-on-error main.tex
xelatex -interaction=nonstopmode -halt-on-error main.tex
```

For an online LaTeX editor, upload the ZIP contents, choose `main.tex` as the main document, and select XeLaTeX. No shell escape or bibliography-processing step is required.

The earlier modular source remains in `../orca-agent-final-report-en/`. This consolidated copy is intended for direct editing; later changes to one copy do not automatically synchronize with the other.

Status: awaiting user review and acceptance.
