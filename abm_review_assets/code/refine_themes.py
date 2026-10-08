from pathlib import Path
from docx import Document
from docx.oxml import OxmlElement
from docx.text.paragraph import Paragraph

root = Path(Path("<PROJECT_ROOT>"))
doc = Document(root / "review_current.docx")

def replace_exact_start(start, text):
    para = next(p for p in doc.paragraphs if p.text.startswith(start))
    para.text = text
    return para

def insert_after(anchor, text, style="Normal"):
    node = OxmlElement("w:p")
    anchor._p.addnext(node)
    para = Paragraph(node, anchor._parent)
    para.style = style
    para.add_run(text)
    return para

replace_exact_start(
    "Epidemiology and public health constitute the largest identified domain",
    "The thematic profile combines a broad application base with a small number of prominent domains. Epidemiology and public health is the largest identified domain (7,271 papers; 24.95%), followed by simulation methodology (4,986; 17.11%) and ecology and environment (4,564; 15.66%). Among application domains, epidemiology/public health and ecology/environment together account for 40.62% of the corpus. Transport (9.79%), social and opinion dynamics (8.53%), and urban and land-use systems (6.70%) form a second tier. This distribution reflects the fit between ABM and questions in which heterogeneous entities interact and generate aggregate outcomes. The substantial methodological category also shows that the field’s growth is not limited to applications: researchers continue to develop and refine ABM as a modeling approach."
)

replace_exact_start(
    "The time series shows a clear rebalancing.",
    "The annual series indicates a change in the relative composition of the field, not simply uniform growth across topics. The share of ecology and environment declined from approximately 28.0% in 2001 to about 15% in 2025. Epidemiology and public health rose from roughly 15.3% in 2001, peaked at 40.9% in 2022, and remained the leading application family through 2025. Transport also gained share, from about 2–3% around 2000 to approximately 11% in 2025, while social and opinion dynamics became an established line of work. Taken together, these trends point to a broader portfolio in which human behavior, institutions, mobility, communication, and policy are increasingly studied alongside environmental processes; they do not imply that earlier domains have disappeared."
)

fig5 = next(p for p in doc.paragraphs if p.text.startswith("Figure 5. Subtheme composition"))
insert_after(fig5,
    "The subtheme profile adds detail beneath these broad domains (Figure 5). Ecology, evolution, and population biology account for 10,014 mentions (15.2%), followed by infectious disease and epidemiology (8,660; 13.2%) and social dynamics and behaviour (7,595; 11.6%). Health services and clinical research (8.1%) and transport and mobility (6.4%) are also substantial. The figure counts subtheme mentions rather than unique papers: each paper can carry two or three labels, so the 65,733 mentions exceed the corpus size and the percentages should be read as shares of all assigned mentions. The “Other” category is an aggregate of less frequent subthemes, not a single substantive topic."
)

fig6 = next(p for p in doc.paragraphs if p.text.startswith("Figure 6. Annual evolution of subtheme"))
fig6.text = "Figure 6. Annual evolution of subtheme composition, 1997–2025."
insert_after(fig6,
    "The annual subtheme series shows how this diversification unfolded. Ecology-, evolution-, and population-biology mentions occupy a smaller share of the annual mix in recent years, while infectious-disease and epidemiological work and social and behavioural topics take up larger shares. Smaller but visible contributions from energy, urban systems, networks, and modeling methods further illustrate that ABM is being applied across connected problem areas. Because the chart reports within-year shares of subtheme mentions, these shifts describe changes in emphasis; they should not be interpreted on their own as absolute declines or increases in publication counts."
)

replace_exact_start(
    "The cross-domain co-occurrence structure reinforces this interpretation.",
    "Cross-domain co-occurrence provides a complementary view of this breadth. Epidemiology/public health co-occurs with health-care systems in 4,476 papers; ecology/environment co-occurs with policy and disaster response in 2,585; and urban/land-use planning co-occurs with transport in 1,939 and energy systems in 1,075. These pairings suggest that many ABM studies sit at the intersection of application areas rather than within isolated topical silos. They map where topics appear together in the corpus, although co-occurrence alone does not establish that the papers integrate those systems in the same model or that one domain influences another."
)

replace_exact_start(
    "The post-2022 period nevertheless shows a distinctive thematic signal.",
    "The pre/post comparison reveals continuity at the top of the subtheme ranking alongside movement elsewhere in the distribution. Epidemiological modeling and opinion dynamics remain ranked first and second in both periods. Social network analysis moves from fourth to third, social influence from fifth to fourth, urban planning from eleventh to sixth, and ABM as a named method from twelfth to eighth; information diffusion also enters the post-release top twenty. By contrast, population dynamics shifts from third to fifth, disease-spread modeling from sixth to eighteenth, cost-effectiveness from seventh to twelfth, and wildlife conservation from tenth to twentieth. These are relative ranks among the leading subthemes, not direct measures of absolute publication volume. The pattern is consistent with greater visibility for networked interaction, influence, and information-related questions, but the descriptive comparison cannot attribute these changes to LLMs or to a single historical event."
)

# The table itself follows Figure 7 in the document body, while its caption is
# stored after the table; start the whole table on a fresh page to avoid a split.
table2 = next(t for t in doc.tables if t.cell(0, 0).text.strip() == "Dimension")
break_p = OxmlElement("w:p")
ppr = OxmlElement("w:pPr")
page_break = OxmlElement("w:pageBreakBefore")
ppr.append(page_break)
break_p.append(ppr)
table2._tbl.addprevious(break_p)

out = root / "agent_based_modeling_review_revised_v2.docx"
doc.save(out)
print(out)

