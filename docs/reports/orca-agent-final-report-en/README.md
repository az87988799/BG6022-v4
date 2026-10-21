# Auto-ORCA: English Academic Manuscript

English translation of the current working Chinese manuscript at `../orca-agent-final-report-zh/main.tex`, including its two input files, prepared on 21 October 2026 (Asia/Hong_Kong). The title-page author and PDF author metadata are **CHEN GENG**. No affiliation, correspondence address, or publication date has been invented.

## Files and compilation

- `main.tex`: manuscript entry point and English article formatting.
- `introduction-theory.tex`: abstract and Sections 1–3.
- `architecture-workflow.tex`: Sections 4–5.
- `methods-discussion.tex`: Sections 6–9, with the results file included in Section 7.
- `v4-calculation-results.tex`: the seven scientific test cases and their result tables.
- `references.tex`: the original bibliography, with documentary descriptions and dates translated.
- `figures/`: the three original English-language diagrams, copied without changes.
- `final-report-en-academic.pdf`: the compiled 25-page manuscript.
- `validation.json`: translation-integrity and build checks, with source and deliverable hashes.

Keep the directory contents together. From this directory, compile using a standard TeX Live installation:

```powershell
xelatex -interaction=nonstopmode -halt-on-error main.tex
xelatex -interaction=nonstopmode -halt-on-error main.tex
```

The project uses the standard `article` class, English hyphenation, Latin Modern fonts, numbered citations, and author placement below the title. The built-in desktop LaTeX editor also compiles this project. No external bibliography processor is required. Auxiliary files and the local source ZIP are excluded from Git.

## Scope and verification

The translation preserves all nine sections, 38 subsections, 14 tables, three figures, eight displayed equations, 25 bibliography entries, reference keys, scientific numerical values, and stated evidential limitations. Decimal data and normalized displayed equations were compared against the Chinese sources. All figures retain identical SHA-256 hashes. English-language and visual reviews covered the manuscript; title-page authorship and PDF metadata were checked.

Editorial adjustments are limited to academic English, English table widths, removal of the duplicated title prefix and one duplicated sentence, and terminology clarification. The source mentions an uncomputed T3 endpoint without identifying it in the result subsection. The translation retains that limitation but removes the inaccurate assertion that the result subsection explicitly identifies it; no endpoint angle is inferred. Existing numerical and bibliographic claims were translated rather than independently revalidated.

This is a document translation and typesetting task. It does not modify the runtime, rerun ORCA calculations, make new scientific validation claims, or change the Chinese manuscript. Existing local changes outside this directory are excluded from this delivery.

Status: **awaiting user review and acceptance**.
