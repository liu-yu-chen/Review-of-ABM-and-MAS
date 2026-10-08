from pathlib import Path
from docx import Document
from docx.oxml import OxmlElement
from docx.text.paragraph import Paragraph
import re

SRC = Path(Path("<INPUT_DOCX>"))
OUT = Path(Path("<INPUT_DOCX>"))
doc = Document(SRC)

def edit_start(prefix, replacement):
    matches = [p for p in doc.paragraphs if p.text.startswith(prefix)]
    if len(matches) != 1:
        raise ValueError(f"Expected one paragraph for {prefix!r}; got {len(matches)}")
    if replacement == "":
        matches[0]._element.getparent().remove(matches[0]._element)
    else:
        matches[0].text = replacement
    return matches[0]

edits = [
    ("Agent-based modeling (ABM) has moved from a specialized approach",
     "Agent-based modeling (ABM) has developed from a specialized approach to representing heterogeneous interacting entities into a widely used framework for studying coupled social, biological, urban, and technological systems. This review combines a corpus analysis of 29,139 publications from 1972 to 2025 with established research on ABM design, large language model (LLM)-based agents, and scientific collaboration networks. Three findings emerge. First, annual publication output rose from 134 papers in 2000 to 2,495 in 2025, alongside a thematic shift from ecology and the environment toward epidemiology, public health, social dynamics, transport, and urban systems. Second, LLM adoption is measurable but remains at an early stage: the share of ABM papers using LLMs increased from 1.50% in 2023 to 5.65% in 2025. Of the identified uses, 59.6% involved agent behavior or decision-making, while 40.4% supported implementation, data generation, analysis, or interfaces. Third, collaboration became more extensive and international, yet also more modular: detected communities increased from 8 to 52 across the analyzed periods, with no evidence of general convergence. Together, these findings show how application priorities, agent architectures, and research communities are changing. They also point to a shared agenda: strengthen behavioral validation, transparency, reproducibility, and the evaluation of LLM-enabled agents."),
    ("ABM is a computational approach in which system-level outcomes emerge",
     "Agent-based modeling (ABM) explains system-level outcomes through the actions and interactions of heterogeneous agents situated in an environment. Its distinctive contribution is to make the micro-to-macro pathway explicit: agents perceive conditions, follow decision rules, interact within a network or space, and adapt or learn; their interactions can then generate patterns not specified at the aggregate level. ABM is especially useful when heterogeneity, path dependence, feedback, spatial structure, or bounded rationality is central to the research question. [[FN01]]"),
    ("The empirical backbone of this review is the supplied corpus analysis",
     "This review draws on a corpus analysis of 29,139 publications dated 1972–2025. Titles and abstracts were classified by application domain, modeled entity, LLM involvement and function, authorship, and country. The analysis combined trend tests, logistic models with Benjamini–Hochberg correction, counterfactual Poisson analyses, and coauthorship community detection. The corpus should therefore be understood as the empirical basis for this review, not as an exhaustive census of every ABM publication. LLM findings in particular are early-diffusion estimates: the post-release observation window covers only 2023–2025. Records were collected from Web of Science, PubMed, and DBLP, deduplicated, and, where possible, enriched with abstracts through OpenAlex. [[FN02]] The cleaning workflow excluded records that still lacked abstracts after enrichment, non-research publication types (including editorials, correspondence, and reviews), and non-English publications. The resulting strict corpus contains 29,139 publications through 2025; the broader all-years corpus contains more than 31,000 records. Figure 1 summarizes the collection and filtering process."),
    ("To make the corpus construction auditable",
     "Figure 1 distinguishes the all-years record pool from the publication-year cutoff used in the analyses, making the corpus construction and reporting window explicit."),
    ("For corpus-level classification, records with abstracts",
     "Corpus-level classification used the DeepSeek API through an OpenAI-compatible asynchronous client and the deepseek-chat model. Each request included a paper’s title and abstract; abstracts were truncated at 6,000 characters, and records without an abstract were not submitted for classification. Requests used temperature 0, JSON-object output, and a 512-token response limit. The asynchronous run used checkpoints and retried eligible failures, allowing processing to resume without replacing completed results."),
    ("The returned JSON was parsed and checked",
     "The returned JSON was parsed and checked before being merged with the corpus. This constrained design improves consistency and auditability, but it does not remove classification uncertainty or the need for human validation."),
    ("Prompt engineering was used to constrain the task",
     ""),
    ("The long-run trajectory of ABM research is characterized",
     "ABM research expanded steadily and diversified across disciplines. The corpus grew approximately nineteen-fold between 2000 and 2025, marking a shift from a specialized simulation approach to a framework used across many research areas. The increase was not a single abrupt departure from the previous trend. A segmented Poisson model identified 2021 as a potential breakpoint and estimated a 24.0% increase in publication intensity (z = 6.36, p = 2.0 × 10−10). Yet observed output in 2021–2023 (3,774 papers) was close to the pre-breakpoint projection (3,945 papers), with no significant excess (rate ratio = 0.957, p = 0.997). After adjustment for overall retrieval trends, the estimated change associated with this high-growth period was also not significant (−1.8%, p = 0.846)."),
    ("These results suggest that the expansion of ABM research",
     "Taken together, the analyses are more consistent with cumulative diffusion across scientific domains than with a single external shock. Publication growth alone, however, cannot show how the field itself changed. The next sections therefore examine domain composition and thematic shifts, separating expansion in the number of papers from redistribution of attention across research areas."),
    ("The thematic profile combines a broad application base",
     "The corpus spans a broad range of applications, although a few domains account for much of the research. Epidemiology and public health is the largest identified domain (7,271 papers; 24.95%), followed by simulation methodology (4,986; 17.11%) and ecology and environment (4,564; 15.66%). The two leading application domains—epidemiology/public health and ecology/environment—together represent 40.62% of the corpus. Transport (9.79%), social and opinion dynamics (8.53%), and urban and land-use systems (6.70%) form a second tier. This distribution is consistent with ABM’s value for studying interactions among heterogeneous entities and their aggregate consequences. The substantial methodology category also shows that growth includes continued work on ABM itself, not applications alone."),
    ("The annual series indicates a change in the relative composition",
     "The annual series shows that the field’s composition changed alongside its overall growth. Ecology and environment declined from about 28.0% of papers in 2001 to roughly 15% in 2025. Epidemiology and public health rose from about 15.3% in 2001, peaked at 40.9% in 2022, and remained the leading application family through 2025. Transport also gained share—from roughly 2–3% around 2000 to about 11% in 2025—while social and opinion dynamics became an established line of work. These changes indicate a broader portfolio in which human behavior, institutions, mobility, communication, and policy are studied alongside environmental processes; they do not mean that earlier areas have disappeared."),
    ("The subtheme profile adds detail beneath these broad domains",
     "Subthemes reveal more detail within the broad domains (Figure 5). Ecology, evolution, and population biology account for 10,014 mentions (15.2%), followed by infectious disease and epidemiology (8,660; 13.2%) and social dynamics and behaviour (7,595; 11.6%). Health services and clinical research (8.1%) and transport and mobility (6.4%) are also prominent. These are subtheme mentions, not unique papers: each paper can receive two or three labels. The 65,733 total mentions therefore exceed the corpus size, and percentages represent shares of assigned mentions. “Other” combines less frequent subthemes rather than denoting one substantive topic."),
    ("The annual subtheme series shows how this diversification unfolded",
     "The annual subtheme series traces this diversification over time. Ecology, evolution, and population biology make up a smaller share of recent annual mentions, while infectious disease and epidemiology and social and behavioural topics account for larger shares. Energy, urban systems, networks, and modeling methods remain smaller but visible parts of the mix. Because the figure reports each year’s share of subtheme mentions, it describes changes in relative emphasis, not absolute publication counts."),
    ("Cross-domain co-occurrence provides a complementary view",
     "Topic co-occurrence offers another view of the corpus’s breadth. Epidemiology/public health co-occurs with health-care systems in 4,476 papers; ecology/environment with policy and disaster response in 2,585; and urban/land-use planning with transport in 1,939 and energy systems in 1,075. These pairings show that many papers are indexed across intersecting application areas rather than isolated topics. Co-occurrence does not, by itself, establish that the topics are integrated within the same model or that one domain influences another."),
    ("The co-occurrence structure also reflects a distinctive feature",
     "This co-occurrence structure reflects ABM’s transferability across disciplinary settings. Its core concepts—heterogeneous agents, local interaction, adaptation, and emergence—can be used to study many kinds of complex systems. The observed links among health, urban systems, transport, environmental processes, and social dynamics therefore suggest that an increasing share of the literature addresses coupled problems. Some models may represent interactions among people, institutions, infrastructure, and the environment, allowing researchers to examine feedback and unintended consequences that aggregate approaches can obscure. The co-occurrence results indicate topical overlap, however, and should not be taken as direct evidence that every paper models these mechanisms jointly."),
    ("Humans and households comprise 41.20%",
     "Humans and households account for 41.20% of modeled-entity classifications, while patients and other biological entities account for 22.34%. Decade-based logistic models show a strong increase in human/household representation (OR = 1.483 per decade, q = 1.3 × 10−57) and declines in generalized network nodes, software/computational agents, patients/biological entities, and swarm robots. This shift does not imply that computational or biological models are disappearing. Rather, it signals a growing emphasis on coupled human–environment and human–technology systems, where decisions and interaction networks are themselves part of the causal explanation."),
    ("This shift also clarifies a methodological tension",
     "The shift toward human-centered applications sharpens a methodological tension: richer behavioral representations also increase the burden of validation. The ODD protocol and its updates make model structure, design concepts, and implementation details inspectable. As behavioral assumptions become more elaborate, transparent reporting becomes more important, not less. [[FN03]]"),
    ("The pre/post comparison reveals continuity at the top",
     "The pre/post comparison shows continuity among the highest-ranked subthemes alongside changes elsewhere in the distribution. Epidemiological modeling and opinion dynamics remain first and second in both periods. Social network analysis rises from fourth to third, social influence from fifth to fourth, urban planning from eleventh to sixth, and ABM as a named method from twelfth to eighth; information diffusion also enters the post-release top twenty. In contrast, population dynamics moves from third to fifth, disease-spread modeling from sixth to eighteenth, cost-effectiveness from seventh to twelfth, and wildlife conservation from tenth to twentieth. These are relative rankings among leading subthemes, not measures of absolute publication volume. The pattern is consistent with greater visibility for networked interaction, influence, and information-related questions, but this descriptive comparison cannot attribute the changes to LLMs or to any single historical event."),
    ("LLM use is growing rapidly from a small base",
     "LLM use grew rapidly from a small base. Of the 29,139 papers in the corpus through 2025, 236 (0.81%) were classified as using an LLM. Annual counts increased from 32 in 2023 to 63 in 2024 and 141 in 2025. Using the annual denominator applied in the corpus trend analysis, the corresponding shares rose from 1.50% to 2.75% and 5.65%, a 2025-to-2023 rate ratio of 3.77. Despite this clear increase, LLM use was not standard practice by 2025: most ABM papers in the observation window were not classified as LLM-enabled. Because the post-release series covers only three years, it should be read as an early-diffusion snapshot, not a settled trajectory."),
    ("The function of the LLM matters as much as its presence",
     "The LLM’s function is as important as its presence. Of 235 papers with a classifiable role, 140 (59.6%; Wilson 95% CI 53.2–65.6) used the model in agent behavior or decision-making. The other 95 papers (40.4%) used LLMs in supporting roles, including code or model implementation, data and parameter generation, evaluation or text analysis, and natural-language interfaces. The corpus thus shows two concurrent patterns: LLMs can form part of the simulated decision mechanism, or support the research workflow around it. These roles should be distinguished because only the former directly changes how simulated agents generate behavior."),
    ("The most consequential change introduced by LLMs",
     "The key transition is the use of LLMs within agents’ decision processes, rather than only as tools for programming, text analysis, or data processing. Conventional ABMs typically specify decision rules directly; LLM-based agents can instead interpret context, retrieve memories, plan, and interact in natural language. Such capabilities may represent flexible, context-dependent behavior that fixed rules are less suited to capture. They do not, on their own, establish that the resulting behavior is valid. [[FN04]]"),
    ("Existing studies suggest that LLM-enabled ABM",
     "The available evidence supports treating LLM-enabled ABM as an emerging extension of established ABM, not as its replacement. LLM-driven agents still need to represent bounded rationality, adaptation, spatial and network constraints, and institutional rules, and their behavior must be calibrated and validated against empirical evidence. Linguistically coherent, contextually plausible responses are not necessarily stable or behaviorally valid: agents may produce inconsistent choice probabilities, weak demographic differentiation, or implausible policy responses. Outcomes may also vary with model version, prompt, context, stochastic settings, and knowledge-updating procedures. These issues are especially consequential when an LLM serves as the decision engine, because even small changes can alter the simulated behavioral mechanism. [[FN05]]"),
    ("Future research should establish more systematic frameworks",
     "Future research should make uncertainty and validity assessable. Studies should compare LLM-based agents with rule-based and empirically calibrated baselines, then test sensitivity to model version, prompt, random seed, temperature, and information-retrieval strategy. Validation should draw on observed behavior, experiments, or historical cases and should assess both individual decisions and aggregate outcomes. Researchers should also report computational costs, data provenance, privacy safeguards, and how generated outputs were reviewed. Current applications are concentrated in a limited set of areas and cover only the first years of adoption; cross-domain replications and longer time series are therefore needed before general claims about LLMs’ value in ABM are justified."),
    ("LLM-enabled ABM is not evenly distributed",
     "LLM-enabled ABM is concentrated in a few application areas. Among the 235 papers with classifiable roles in Figure 14, social and opinion dynamics is the largest group (81 papers; 34.5%), followed by health and epidemiology (58; 24.7%). Other prominent areas are AI and multi-agent computing (28; 11.9%), policy and disaster response (25; 10.6%), sustainability and energy (21; 8.9%), and transport and urban mobility (18; 7.7%). Networks and communication and other specialized domains contribute two papers each. This distribution places current work chiefly in social-behavioral and health research, alongside a smaller technical strand in AI and multi-agent systems. Decision-making is the most common LLM function across the major domains; coding, data generation, analysis, and interfaces provide additional routes of use."),
    ("The simulated entities in LLM-related studies also differ",
     "The agent types represented in LLM-related studies also differ from those in the broader ABM corpus. Software or computational agents are 11.5 percentage points more prevalent, while biological entities are 12.7 points less prevalent, in the LLM subset. This is consistent with the emergence of software and LLM-based agents as objects of study, but it does not mean that every paper using an LLM simulates an LLM agent. Some use the model to support decisions, data preparation, analysis, or interaction for a different agent population. Separating the LLM’s function from the simulated agent type is therefore essential to interpreting the field’s development."),
    ("The review proposes a four-part evaluation protocol",
     "The review proposes evaluating LLM-enabled ABM along four dimensions. Behavioral validity asks whether an LLM agent reproduces observed choices, response times, network ties, and switching behavior. Process validity examines whether mechanisms such as memory, planning, social influence, and adaptation remain stable under changes to prompts, seeds, models, or temperature. Macro-level validity tests whether aggregate patterns and policy responses match historical or experimental benchmarks. Governance validity concerns provenance, disclosure of synthetic decisions, protection of sensitive data, and replicability. This framework complements ODD-style documentation with model cards, prompt and version records, stochastic controls, and behavioral benchmarks."),
    ("ABM is increasingly a team science",
     "ABM increasingly depends on team-based research. Mean authorship rose from 1.86 authors per paper in 1996 to 4.07 in 2025 (+118.8%); the share of multi-author papers increased from 55.8% to 77.3%, and international collaboration from 3.9% to 32.1%. This growth is consistent with the field’s increasing need to combine domain expertise with data engineering, software development, and policy interpretation. [[FN06]]"),
    ("The network, however, does not simply become more integrated",
     "Yet a larger network is not necessarily a more integrated one. Weighted Louvain detection identified 37 communities in the core coauthorship network (modularity Q = 0.8939). Across time windows, the number of communities rose from 8 in 1996–2005 to 52 in 2016–2025, while modularity increased from 0.755 to 0.9136. Community persistence weakened: the adjusted Rand index fell from 1.00 in the first comparison to 0.2906 in the last, while mergers remained limited. The most cautious interpretation is selective expansion accompanied by stronger community boundaries, rather than the emergence of one integrated ABM community."),
    ("This finding is important because collaboration density",
     "Collaboration density and intellectual integration are distinct. A field can add authors, countries, and coauthorship ties while becoming more specialized. Community detection is sensitive to network construction, resolution limits, and the selected core authors. The present analysis therefore treats detected communities as a map of knowledge production, not a definitive taxonomy of scientific identity. [[FN07]]"),
    ("Although the core network places influential scholars",
     "The core network places scholars such as Nigel Gilbert, Bruce Edmonds, Joshua M. Epstein, Dirk Helbing, Michael Batty, Andrew Crooks, and Michael W. Macy in structurally central positions. This pattern is consistent with ABM’s development as a multi-center field rather than one organized around a single intellectual hub. Major clusters have formed around urban and spatial simulation, epidemiology, environmental management, transport, social dynamics, and policy modeling, each bringing different disciplinary perspectives to the field."),
    ("To characterize the roles of individual researchers",
     "Researchers’ contributions are best interpreted through complementary indicators rather than a single ranking. Publication output reflects sustained activity and engagement with ABM. Normalized impact provides a different view by accounting for factors such as large teams, highly cited methodological work, and citation differences between ABM and adjacent disciplines. Network position captures how scholars connect to collaborators and facilitate knowledge exchange across research clusters."),
    ("Therefore, no single indicator can independently determine",
     "No one indicator can establish who contributes most to ABM. High output may signal a productive research program; high normalized impact may reflect influential work; and network centrality may indicate a role in connecting communities. Considering these dimensions together gives a more balanced account than publication or citation counts alone. This distinction matters particularly for emerging areas such as LLM-enabled ABM, where methodological innovation and integration with existing communities may be as consequential as publication volume."),
    ("Coauthorship communities are not only an organizational phenomenon",
     "Coauthorship communities can shape research practice as well as reflect it. Closely connected groups may share benchmarks, software, ontologies, and validation norms; less connected groups may independently develop similar mechanisms under different labels. The high modularity observed here suggests that future progress will depend partly on translation infrastructure: shared model descriptions, interoperable agent schemas, common behavioral benchmarks, and cross-community replication."),
    ("LLMs may either weaken or strengthen these boundaries",
     "LLMs could either bridge or reinforce these boundaries. They may help translate concepts across disciplines through natural-language interfaces, but they may also foster a separate body of work whose agents are difficult to compare because prompts, models, memory architectures, and evaluation criteria differ. Interoperability is therefore both a technical challenge and a question about how research communities exchange methods."),
    ("The corpus-building process illustrates both the practical value",
     "The corpus-building process illustrates both the value and the limits of LLM-assisted review. Language models can help screen and organize evidence across more publications than reviewers could process manually at the same speed. Used alongside database searches and explicit eligibility criteria, they can support consistent classification of topics, modeled entities, and methodological roles in titles and abstracts. Their semantic capabilities may also distinguish related themes that exact keyword searches or fixed dictionaries overlook, improving thematic discrimination beyond surface-level matching."),
    ("This advantage should not be equated with guaranteed accuracy",
     "These advantages do not guarantee accuracy. Classification quality depends on corpus coverage, operational definitions, model and prompt stability, and the evidence available in each record. Screening and coding decisions therefore remain open to audit. LLM assistance can shift rather than remove human work: reviewers must examine ambiguous cases, resolve disagreements, verify citations and extracted claims, and document inclusion and exclusion decisions. This verification burden can be substantial at scale. Risk-based review offers one practical response: double-code stratified samples and high-impact or uncertain cases, report error types and agreement, and preserve prompts, model versions, and decision logs."),
    ("LLMs can enter ABM at several distinct points",
     "LLMs can contribute to ABM at several distinct stages, and these roles should be distinguished. As an internal agent mechanism, an LLM can interpret context, generate candidate actions, support planning or dialogue, and mediate adaptation. This offers a direct route to language-mediated behavior, but makes decisions sensitive to prompts, model updates, sampling, and context construction. As an external research assistant, an LLM can help translate theory into candidate rules or code, generate tests and scenarios, extract behavioral evidence from documents, or provide natural-language interfaces for inspecting a model. These supporting uses may accelerate research without making the LLM the causal decision mechanism of the simulated agents."),
    ("A robust research design should state explicitly",
     "A sound research design should specify the LLM’s role and where it enters the modeling workflow. For LLM-driven agents, authors should define the agent population and information access, distinguish generated behavior from hard-coded constraints, and test sensitivity to model, prompt, seed, and temperature. Validation should compare individual choices and interactions with empirical or experimental benchmarks, then assess whether aggregate outcomes and policy responses remain credible. For supporting applications, researchers should document and independently verify the provenance of generated code, labels, and synthetic data. In either case, LLMs should remain components of a theory-led, empirically constrained workflow—not substitutes for behavioral theory, calibration, or validation."),
    ("The increasingly modular coauthorship structure points",
     "The increasingly modular coauthorship structure suggests a dual agenda. Specialized communities provide the depth needed to develop domain-specific theory, data, and validation practices. At the same time, weak ties between communities can slow the circulation of reusable mechanisms and standards. Future research should examine not only who collaborates, but how methods travel: whether cross-community teams, shared benchmarks, open model repositories, and replication projects connect epidemiology, transport, environmental systems, and social dynamics. Longitudinal analyses could test whether LLM-enabled ABM papers bridge existing groups or form a distinct cluster, and whether such positions correspond to methodological diffusion rather than publication growth alone."),
    ("These inferences require care",
     "These inferences should be treated cautiously. Community assignments depend on author-name disambiguation, database coverage, network thresholds, and the resolution and time window of the detection algorithm; coauthorship is also an imperfect proxy for intellectual exchange. Follow-up work should test robustness across network specifications and combine coauthorship with complementary evidence, including shared citations, software reuse, institutional ties, and cross-domain publications. The goal is not to eliminate disciplinary boundaries, but to build translation infrastructure—interoperable model descriptions, shared behavioral benchmarks, transparent data and code, and recurring replication exercises—so that specialized groups can compare findings without erasing meaningful differences in theory and context."),
    ("The evidence supports a three-layer account",
     "The evidence points to three connected changes. First, ABM has expanded across health, ecology, mobility, cities, energy, opinion, and policy, particularly where heterogeneous individuals interact within coupled systems. Second, LLMs are entering the modeling process both inside agents’ behavioral and decision mechanisms and as tools for coding, data work, analysis, and interfaces. Third, collaboration has become more extensive and international, while research remains divided among specialized communities."),
    ("The innovation of this synthesis is to treat these layers",
     "These changes are best understood as interdependent rather than sequential. Broader applications create demand for richer behavioral mechanisms; richer mechanisms heighten the need for empirical validation and shared standards; and the collaboration network shapes how those standards spread. The central question is therefore not whether LLMs will replace conventional ABM, but whether the field can incorporate generative decision mechanisms without sacrificing transparency, calibration, and reproducibility."),
    ("Five priorities follow",
     "This synthesis yields five priorities. First, evaluate behavior against empirical benchmarks rather than conversational plausibility alone. Second, report the LLM’s function and position in the model, distinguishing internal decision mechanisms from supporting uses. Third, extend ODD-style reporting to include prompts, model versions, seeds, memory, retrieval, and safeguards. Fourth, develop cross-domain benchmarks that connect epidemiology, mobility, social influence, and policy models. Fifth, examine collaboration networks as channels of methodological diffusion, including whether LLM-enabled papers bridge existing groups or form a weakly connected cluster."),
    ("ABM has entered a mature expansion phase",
     "ABM is in a period of sustained expansion, broad application, human-centered modeling, and increasingly international collaboration. The corpus shows that LLM adoption is rising but remains at an early stage: most ABM papers are not LLM-enabled, and current use is concentrated in a limited set of behavioral and decision functions. Meanwhile, collaboration networks are becoming more modular, making interoperability across specialized communities an important condition for future progress. The field’s next advance will not be measured by adoption alone, but by whether generative agents can be empirically validated, theoretically interpreted, computationally reproduced, and compared across research communities."),
]

for prefix, replacement in edits:
    edit_start(prefix, replacement)

doc.paragraphs[0].text = "Agent-Based Modeling: Research Trends, LLM Adoption, and Collaboration Networks"
doc.paragraphs[1].text = "A quantitative review of publications, 1972–2025"
doc.paragraphs[2]._element.getparent().remove(doc.paragraphs[2]._element)

# Add citations for works previously present in the reference list but not cited.
# Move all author–date markers into dedicated placeholder runs for true footnotes.
citations = {
    "(Bonabeau, 2002; Macal & North, 2010)": "[[FN10]]",
    "(Grimm et al., 2006, 2010)": "[[FN11]]",
    "(Park et al., 2023)": "[[FN12]]",
    "(Ghaffarzadegan et al., 2024; Li et al., 2024)": "[[FN13]]",
    "(Fortunato & Barthélemy, 2007; Fortunato, 2010)": "[[FN14]]",
}
# Citation-bearing text was rewritten above using FN placeholders for the first two groups.
# Retain the other source groups, with review sources added to the LLM-agent note and
# Newman (2004, 2006) added to the collaboration/network-method note.
for p in doc.paragraphs:
    txt = p.text
    if "Park et al., 2023" in txt:
        txt = txt.replace("(Park et al., 2023)", "[[FN12]]")
    if "Ghaffarzadegan et al., 2024; Li et al., 2024" in txt:
        txt = txt.replace("(Ghaffarzadegan et al., 2024; Li et al., 2024)", "[[FN13]]")
    if "Fortunato & Barthélemy, 2007; Fortunato, 2010" in txt:
        txt = txt.replace("(Fortunato & Barthélemy, 2007; Fortunato, 2010)", "[[FN14]]")
    if txt != p.text:
        p.text = txt

# Attach Park and the two LLM-agent surveys to the same claim.
for p in doc.paragraphs:
    if p.text.startswith("The key transition is the use of LLMs"):
        p.text = p.text.replace("[[FN04]]", "[[FN12]] [[FN15]]")

# Add a footnote to the collaboration growth sentence, grounding the network framing.
for p in doc.paragraphs:
    if p.text.startswith("ABM increasingly depends on team-based research"):
        p.text = p.text.replace("[[FN06]]", "[[FN16]]")

# Add Newman’s community-detection source to the discussion of method sensitivity.
for p in doc.paragraphs:
    if p.text.startswith("Collaboration density and intellectual integration"):
        p.text = p.text.replace("[[FN07]]", "[[FN14]] [[FN17]]")

# Build citation marker runs so note references are placed exactly at the citation point.
marker_re = re.compile(r"\[\[FN\d+\]\]")
for p in doc.paragraphs:
    if marker_re.search(p.text):
        txt = p.text
        p.clear()
        cursor = 0
        for match in marker_re.finditer(txt):
            if match.start() > cursor:
                p.add_run(txt[cursor:match.start()])
            p.add_run(match.group())
            cursor = match.end()
        if cursor < len(txt):
            p.add_run(txt[cursor:])

# Correct and complete bibliographic metadata; keep the reference list because the
# converted notes use full citations and the bibliography remains useful for lookup.
refs = {
    "Ghaffarzadegan, N., et al. (2024). Generative agent-based modeling: An introduction and tutorial. System Dynamics Review.":
    "Ghaffarzadegan, N., Majumdar, A., Williams, R., & Hosseinichimeh, N. (2024). Generative agent-based modeling: An introduction and tutorial. System Dynamics Review, 40(1), e1761. https://doi.org/10.1002/sdr.1761",
    "Li, G., et al. (2024). LLMs and generative agent-based models for complex systems research. Physics of Life Reviews. https://doi.org/10.1016/j.plrev.2024.10.013":
    "Lu, Y., Aleta, A., Du, C., Shi, L., & Moreno, Y. (2024). LLMs and generative agent-based models for complex systems research. Physics of Life Reviews, 51, 283–293. https://doi.org/10.1016/j.plrev.2024.10.013",
    "OpenAlex. (2022). OpenAlex: A fully-open index of scholarly works, authors, venues, institutions, and concepts. https://doi.org/10.48550/arXiv.2205.01833":
    "Priem, J., Piwowar, H., & Orr, R. (2022). OpenAlex: A fully-open index of scholarly works, authors, venues, and concepts. arXiv:2205.01833. https://doi.org/10.48550/arXiv.2205.01833",
    "Wang, L., Ma, C., Feng, X., Zhang, Z., Yang, H., Zhang, J., et al. (2024). A survey on large language model based autonomous agents. Frontiers of Computer Science, 18, 186345. https://doi.org/10.1007/s11704-024-40131-1":
    "Wang, L., Ma, C., Feng, X., Zhang, Z., Yang, H., Zhang, J., Chen, Z., Tang, J., Chen, X., Lin, Y., Zhao, W. X., Wei, Z., & Wen, J.-R. (2024). A survey on large language model based autonomous agents. Frontiers of Computer Science, 18(6), 186345. https://doi.org/10.1007/s11704-024-40231-1",
}
for p in doc.paragraphs:
    if p.text in refs:
        p.text = refs[p.text]

# Preserve existing headers/footers untouched; add no header/footer content.
doc.save(OUT)
print(OUT)

