Overleaf upload
===============

1. In Overleaf, choose New Project -> Upload Project.
2. Upload this ZIP archive.
3. Set the compiler to XeLaTeX (Menu -> Settings -> Compiler).
4. Compile main.tex.

Project layout
--------------
- main.tex: document setup and section assembly
- sections/: abstract and individually editable manuscript sections
- figures/: figures extracted from the verified Word version
- references.bib: bibliography database

The Word manuscript used footnotes for source citations. Those citations are retained as full-text LaTeX footnotes; references.bib supplies the separate reference list. Page headers and footers are disabled.
