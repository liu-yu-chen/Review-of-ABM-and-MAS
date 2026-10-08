from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
import re
import shutil

from docx import Document
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph
from docx.table import Table

SOURCE = Path(r"D:\ONE DRIVE_PERSONAL\OneDrive\文档\agent_based_modeling_review_revised_v4.docx")
ROOT = Path(r"D:\Literature-Research-Agent\overleaf_abm_project")
ARCHIVE = Path(r"D:\Literature-Research-Agent\overleaf_agent_based_modeling_review.zip")
if ROOT.exists():
    shutil.rmtree(ROOT)
(ROOT / "sections").mkdir(parents=True)
(ROOT / "figures").mkdir()

doc = Document(SOURCE)
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS = {"w": W, "r": R, "a": "http://schemas.openxmlformats.org/drawingml/2006/main"}

def esc(s):
    repl = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}
    return "".join(repl.get(c, c) for c in s)

with ZipFile(SOURCE) as z:
    footroot = __import__("lxml.etree", fromlist=["etree"]).fromstring(z.read("word/footnotes.xml"))
    footnotes = {}
    for note in footroot.findall("w:footnote", NS):
        fid = note.get(qn("w:id"))
        if fid in ("-1", "0"):
            continue
        txt = "".join(note.xpath(".//w:t/text()", namespaces=NS)).strip()
        footnotes[fid] = txt

def run_text(run):
    bits = []
    for child in run._r:
        name = child.tag.split("}")[-1]
        if name == "t":
            bits.append(esc(child.text or ""))
        elif name == "tab":
            bits.append(r"\quad ")
        elif name in ("br", "cr"):
            bits.append(r"\\")
        elif name == "footnoteReference":
            note_id = child.get(qn("w:id"))
            note = footnotes.get(note_id, "")
            bits.append(r"\footnote{" + esc(note) + "}")
    s = "".join(bits)
    if run.bold:
        s = r"\textbf{" + s + "}"
    if run.italic:
        s = r"\emph{" + s + "}"
    return s

def paragraph_tex(p):
    return "".join(run_text(r) for r in p.runs).strip()

def plain_paragraph(p):
    return "".join(p._p.xpath(".//w:t/text()"))

def heading_num_strip(s, level):
    if level == 1:
        return re.sub(r"^\s*\d+\.\s*", "", s)
    return re.sub(r"^\s*\d+\.\d+\s*", "", s)

def image_rids(el):
    return el.xpath(".//a:blip/@r:embed")

def block_text(el):
    return "".join(el.xpath(".//w:t/text()")).strip()

# Extract true Word footnotes and embedded PNG figures in document order.
blocks = [el for el in doc.element.body if el.tag in (qn("w:p"), qn("w:tbl"))]
figure_names = {}
figure_count = 0
for el in blocks:
    rids = image_rids(el)
    if not rids:
        continue
    for rid in rids:
        figure_count += 1
        part = doc.part.related_parts[rid]
        suffix = Path(part.partname).suffix.lower() or ".png"
        if suffix not in (".png", ".jpg", ".jpeg", ".pdf"):
            suffix = ".png"
        filename = f"figure_{figure_count:02d}{suffix}"
        (ROOT / "figures" / filename).write_bytes(part.blob)
        figure_names[(id(el), rid)] = filename

def table_tex(table, number, caption):
    rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]
    if not rows:
        return ""
    ncols = len(rows[0])
    if number == 1:
        cols = r">{\raggedright\arraybackslash}p{0.27\linewidth} >{\centering\arraybackslash}p{0.12\linewidth} X"
        size = r"\small"
    else:
        cols = r">{\raggedright\arraybackslash}p{0.15\linewidth} *{4}{>{\raggedright\arraybackslash}X}"
        size = r"\scriptsize"
    lines = [r"\begin{table}[htbp]", r"\centering", size, r"\setlength{\tabcolsep}{3pt}", r"\renewcommand{\arraystretch}{1.18}",
             rf"\setcounter{{table}}{{{number-1}}}", rf"\caption{{{esc(caption)}}}", rf"\label{{tab:table{number}}}", rf"\begin{{tabularx}}{{\linewidth}}{{{cols}}}", r"\toprule"]
    for idx, row in enumerate(rows):
        vals = [esc(x).replace("\n", r"\newline ") for x in row[:ncols]]
        if idx == 0:
            vals = [r"\textbf{" + x + "}" for x in vals]
        lines.append(" & ".join(vals) + r" \\")
        if idx == 0:
            lines.append(r"\midrule")
    lines += [r"\bottomrule", r"\end{tabularx}", r"\end{table}"]
    return "\n".join(lines)

section_files = {
    "1. Scope and analytical design": "01_scope_and_methods.tex",
    "2. Research trends in agent-based modeling": "02_research_trends.tex",
    "3. Large language model adoption in agent-based modeling": "03_llm_adoption.tex",
    "4. Collaboration networks and community structure": "04_collaboration_networks.tex",
    "5. Discussion": "05_discussion.tex",
    "6. Synthesis and research agenda": "06_synthesis.tex",
    "7. Conclusion": "07_conclusion.tex",
}
section_data = {k: [] for k in section_files}

# Abstract is kept separate from numbered sections.
paragraphs = doc.paragraphs
abstract_start = next(i for i, p in enumerate(paragraphs) if p.text.strip() == "Abstract")
scope_start = next(i for i, p in enumerate(paragraphs) if p.text.strip() == "1. Scope and analytical design")
abstract_body = []
for p in paragraphs[abstract_start+1:scope_start]:
    if p.text.strip():
        text = paragraph_tex(p)
        if p.text.startswith("Keywords:"):
            label, value = p.text.split(":", 1)
            abstract_body.append(r"\par\smallskip\noindent\textbf{" + esc(label) + ":} " + esc(value.strip()))
        else:
            abstract_body.append(text)
(ROOT / "sections" / "abstract.tex").write_text("\n\n".join(abstract_body), encoding="utf-8")

current = None
consumed = set()
table_number = 0
for idx, el in enumerate(blocks):
    if idx in consumed:
        continue
    if el.tag == qn("w:p"):
        p = Paragraph(el, doc)
        raw = p.text.strip()
        if p.style.name == "Heading 1":
            if raw == "References":
                current = None
                break
            if raw in section_files:
                current = raw
                title = heading_num_strip(raw, 1)
                section_data[current].append(r"\section{" + esc(title) + "}")
            continue
        if current is None:
            continue
        if not raw and not image_rids(el):
            continue
        if p.style.name == "Heading 2":
            section_data[current].append(r"\subsection{" + esc(heading_num_strip(raw, 2)) + "}")
            continue
        rids = image_rids(el)
        if rids:
            # The supplied manuscript places each figure caption in the next paragraph.
            caption_text = ""
            for j in range(idx+1, len(blocks)):
                if blocks[j].tag == qn("w:p"):
                    maybe = block_text(blocks[j])
                    if maybe.startswith("Figure "):
                        caption_text = maybe
                        consumed.add(j)
                    break
            m = re.match(r"Figure\s+(\d+)\.\s*(.*)", caption_text)
            fig_no = int(m.group(1)) if m else figure_count
            cap = m.group(2) if m else caption_text
            rid = rids[0]
            fname = figure_names[(id(el), rid)]
            width = r"0.88\textwidth" if fig_no in (1, 15, 16, 18, 19, 22, 23) else r"0.96\textwidth"
            section_data[current].append("\n".join([
                r"\begin{figure}[htbp]", r"\centering", rf"\includegraphics[width={width},height=0.78\textheight,keepaspectratio]{{figures/{fname}}}",
                rf"\setcounter{{figure}}{{{fig_no-1}}}", rf"\caption{{{esc(cap)}}}", rf"\label{{fig:figure{fig_no}}}", r"\end{figure}"
            ]))
            continue
        # Preserve the structured prompt's list formatting.
        has_num = el.xpath("./w:pPr/w:numPr")
        if has_num:
            section_data[current].append(r"\begin{itemize}[leftmargin=1.6em,itemsep=2pt,topsep=3pt]" if not section_data[current] or not section_data[current][-1].endswith("\\begin{itemize}[leftmargin=1.6em,itemsep=2pt,topsep=3pt]") else "")
            section_data[current].append(r"\item " + paragraph_tex(p))
            # Add a closing marker; coalesce adjacent numbered paragraphs below.
            section_data[current].append("__LIST_CONTINUES__")
        else:
            if section_data[current] and section_data[current][-1] == "__LIST_CONTINUES__":
                section_data[current].pop()
                section_data[current].append(r"\end{itemize}")
            section_data[current].append(paragraph_tex(p))
    else:
        if current is None:
            continue
        table = Table(el, doc)
        cap_text = ""
        for j in range(idx+1, len(blocks)):
            if blocks[j].tag == qn("w:p"):
                maybe = block_text(blocks[j])
                if maybe.startswith("Table "):
                    cap_text = maybe
                    consumed.add(j)
                break
        match = re.match(r"Table\s+(\d+)\.\s*(.*)", cap_text)
        table_number = int(match.group(1)) if match else table_number + 1
        cap = match.group(2) if match else cap_text
        section_data[current].append(table_tex(table, table_number, cap))

# Close any list left open at section end, then write each separately editable section.
for key, chunks in section_data.items():
    cleaned = []
    in_list = False
    for i, c in enumerate(chunks):
        if c == r"\begin{itemize}[leftmargin=1.6em,itemsep=2pt,topsep=3pt]":
            if not in_list:
                cleaned.append(c); in_list = True
        elif c == "__LIST_CONTINUES__":
            next_is_list = i + 1 < len(chunks) and chunks[i + 1] == r"\begin{itemize}[leftmargin=1.6em,itemsep=2pt,topsep=3pt]"
            if in_list and not next_is_list:
                cleaned.append(r"\end{itemize}"); in_list = False
        elif c == r"\end{itemize}":
            if in_list:
                cleaned.append(c); in_list = False
        else:
            cleaned.append(c)
    if in_list:
        cleaned.append(r"\end{itemize}")
    (ROOT / "sections" / section_files[key]).write_text("\n\n".join(x for x in cleaned if x), encoding="utf-8")

main = r'''% !TeX program = xelatex
\documentclass[11pt]{article}
\usepackage[a4paper,margin=1in]{geometry}
\usepackage{fontspec}
\setmainfont{TeX Gyre Termes}
\usepackage{graphicx}
\usepackage{booktabs}
\usepackage{tabularx}
\usepackage{array}
\usepackage{caption}
\usepackage{float}
\usepackage[section]{placeins}
\usepackage[bottom,hang]{footmisc}
\usepackage{enumitem}
\usepackage{microtype}
\usepackage{hyperref}
\hypersetup{colorlinks=true,linkcolor=black,citecolor=black,urlcolor=blue}
\setlength{\parindent}{0pt}
\setlength{\parskip}{0.55em}
\setcounter{secnumdepth}{2}
\captionsetup{font=small,labelfont=bf}
\pagestyle{empty}

\title{Agent-Based Modeling: Research Trends, LLM Adoption, and Collaboration Networks}
\author{}
\date{A quantitative review of publications, 1972--2025}

\begin{document}
\maketitle
\begin{abstract}
\input{sections/abstract}
\end{abstract}

\input{sections/01_scope_and_methods}
\input{sections/02_research_trends}
\input{sections/03_llm_adoption}
\input{sections/04_collaboration_networks}
\input{sections/05_discussion}
\input{sections/06_synthesis}
\input{sections/07_conclusion}

\section*{References}
\bibliographystyle{apalike}
\nocite{*}
\bibliography{references}
\end{document}
'''
(ROOT / "main.tex").write_text(main, encoding="utf-8")

bib = r'''@article{bonabeau2002,
  author = {Bonabeau, Eric}, title = {Agent-based modeling: Methods and techniques for simulating human systems},
  journal = {Proceedings of the National Academy of Sciences}, year = {2002}, volume = {99}, number = {Suppl. 3}, pages = {7280--7287}, doi = {10.1073/pnas.082080899}
}
@article{fortuna2010,
  author = {Fortunato, Santo}, title = {Community detection in graphs}, journal = {Physics Reports}, year = {2010}, volume = {486}, number = {3--5}, pages = {75--174}, doi = {10.1016/j.physrep.2009.11.002}
}
@article{fortunato2007,
  author = {Fortunato, Santo and Barth{\'e}lemy, Marc}, title = {Resolution limit in community detection}, journal = {Proceedings of the National Academy of Sciences}, year = {2007}, volume = {104}, number = {1}, pages = {36--41}, doi = {10.1073/pnas.0605965104}
}
@article{ghaffarzadegan2024,
  author = {Ghaffarzadegan, Navid and Majumdar, Aritra and Williams, Ross and Hosseinichimeh, Niyousha}, title = {Generative agent-based modeling: An introduction and tutorial}, journal = {System Dynamics Review}, year = {2024}, volume = {40}, number = {1}, pages = {e1761}, doi = {10.1002/sdr.1761}
}
@book{gilbert2005,
  author = {Gilbert, Nigel and Troitzsch, Klaus G.}, title = {Simulation for the Social Scientist}, edition = {2}, publisher = {Open University Press}, year = {2005}
}
@article{grimm2006,
  author = {Grimm, Volker and Berger, Uta and Bastiansen, Finn and Eliassen, Sigrunn and Ginot, Vincent and Giske, Jarl and others}, title = {A standard protocol for describing individual-based and agent-based models}, journal = {Ecological Modelling}, year = {2006}, volume = {198}, number = {1--2}, pages = {115--126}, doi = {10.1016/j.ecolmodel.2006.05.023}
}
@article{grimm2010,
  author = {Grimm, Volker and Berger, Uta and DeAngelis, Donald L. and Polhill, J. Gary and Giske, Jarl and Railsback, Steven F.}, title = {The ODD protocol: A review and first update}, journal = {Ecological Modelling}, year = {2010}, volume = {221}, number = {23}, pages = {2760--2768}, doi = {10.1016/j.ecolmodel.2010.08.019}
}
@article{huang2024,
  author = {Huang, Qian and Vora, Jayesh and Liang, Percy and Leskovec, Jure}, title = {Large language models empowered agent-based modeling and simulation: A survey and perspectives}, journal = {Humanities and Social Sciences Communications}, year = {2024}, volume = {11}, pages = {1259}, doi = {10.1057/s41599-024-03611-3}
}
@article{lu2024,
  author = {Lu, Yikang and Aleta, Alberto and Du, Chunpeng and Shi, Lei and Moreno, Yamir}, title = {LLMs and generative agent-based models for complex systems research}, journal = {Physics of Life Reviews}, year = {2024}, volume = {51}, pages = {283--293}, doi = {10.1016/j.plrev.2024.10.013}
}
@article{macal2010,
  author = {Macal, Charles M. and North, Michael J.}, title = {Tutorial on agent-based modelling and simulation}, journal = {Journal of Simulation}, year = {2010}, volume = {4}, number = {3}, pages = {151--162}, doi = {10.1057/jos.2010.3}
}
@article{newman2004,
  author = {Newman, Mark E. J.}, title = {Coauthorship networks and patterns of scientific collaboration}, journal = {Proceedings of the National Academy of Sciences}, year = {2004}, volume = {101}, number = {Suppl. 1}, pages = {5200--5205}, doi = {10.1073/pnas.0307545100}
}
@article{newman2006,
  author = {Newman, Mark E. J.}, title = {Finding community structure in networks using the eigenvectors of matrices}, journal = {Physical Review E}, year = {2006}, volume = {74}, number = {3}, pages = {036104}, doi = {10.1103/PhysRevE.74.036104}
}
@article{openalex2022,
  author = {Priem, Jason and Piwowar, Heather and Orr, Richard}, title = {OpenAlex: A fully-open index of scholarly works, authors, venues, and concepts}, journal = {arXiv}, year = {2022}, eprint = {2205.01833}, doi = {10.48550/arXiv.2205.01833}
}
@inproceedings{park2023,
  author = {Park, Joon Sung and O'Brien, Joseph C. and Cai, Carrie J. and Morris, Meredith Ringel and Liang, Percy and Bernstein, Michael S.}, title = {Generative agents: Interactive simulacra of human behavior}, booktitle = {Proceedings of the 36th Annual ACM Symposium on User Interface Software and Technology}, year = {2023}, doi = {10.1145/3586183.3606763}
}
@book{railsback2019,
  author = {Railsback, Steven F. and Grimm, Volker}, title = {Agent-Based and Individual-Based Modeling: A Practical Introduction}, edition = {2}, publisher = {Princeton University Press}, year = {2019}
}
@article{wang2024,
  author = {Wang, Lei and Ma, Chen and Feng, Xueyang and Zhang, Zeyu and Yang, Hao and Zhang, Jingsen and Chen, Zhiyuan and Tang, Jiakai and Chen, Xu and Lin, Yankai and Zhao, Wayne Xin and Wei, Zhewei and Wen, Jirong}, title = {A survey on large language model based autonomous agents}, journal = {Frontiers of Computer Science}, year = {2024}, volume = {18}, number = {6}, pages = {186345}, doi = {10.1007/s11704-024-40231-1}
}
'''
(ROOT / "references.bib").write_text(bib, encoding="utf-8")

readme = """Overleaf upload\n===============\n\n1. In Overleaf, choose New Project -> Upload Project.\n2. Upload this ZIP archive.\n3. Set the compiler to XeLaTeX (Menu -> Settings -> Compiler).\n4. Compile main.tex.\n\nProject layout\n--------------\n- main.tex: document setup and section assembly\n- sections/: abstract and individually editable manuscript sections\n- figures/: figures extracted from the verified Word version\n- references.bib: bibliography database\n\nThe Word manuscript used footnotes for source citations. Those citations are retained as full-text LaTeX footnotes; references.bib supplies the separate reference list. Page headers and footers are disabled.\n"""
(ROOT / "README.txt").write_text(readme, encoding="utf-8")

with ZipFile(ARCHIVE, "w", ZIP_DEFLATED) as z:
    for f in ROOT.rglob("*"):
        if f.is_file():
            z.write(f, f.relative_to(ROOT).as_posix())
print(f"Created {ARCHIVE} with {figure_count} figures and {len(section_files)+1} TeX parts")
