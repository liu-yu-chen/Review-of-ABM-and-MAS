from pathlib import Path
import sys

SKILL_SCRIPTS = Path(Path("<DOCX_SKILL_SCRIPTS>"))
sys.path.insert(0, str(SKILL_SCRIPTS))
from insert_note import insert_note

path = Path(Path("<INPUT_DOCX>"))
staging = path.with_name("agent_based_modeling_review_revised_v4_notes.docx")

notes = [
    ("[[FN01]]", "Bonabeau, E. (2002). Agent-based modeling: Methods and techniques for simulating human systems. Proceedings of the National Academy of Sciences, 99(Suppl. 3), 7280–7287. https://doi.org/10.1073/pnas.082080899; Macal, C. M., & North, M. J. (2010). Tutorial on agent-based modelling and simulation. Journal of Simulation, 4(3), 151–162. https://doi.org/10.1057/jos.2010.3; Gilbert, N., & Troitzsch, K. G. (2005). Simulation for the social scientist (2nd ed.). Open University Press; Railsback, S. F., & Grimm, V. (2019). Agent-based and individual-based modeling: A practical introduction (2nd ed.). Princeton University Press."),
    ("[[FN02]]", "Priem, J., Piwowar, H., & Orr, R. (2022). OpenAlex: A fully-open index of scholarly works, authors, venues, and concepts. arXiv:2205.01833. https://doi.org/10.48550/arXiv.2205.01833"),
    ("[[FN03]]", "Grimm, V., Berger, U., Bastiansen, F., Eliassen, S., Ginot, V., Giske, J., et al. (2006). A standard protocol for describing individual-based and agent-based models. Ecological Modelling, 198(1–2), 115–126. https://doi.org/10.1016/j.ecolmodel.2006.05.023; Grimm, V., Berger, U., DeAngelis, D. L., Polhill, J. G., Giske, J., & Railsback, S. F. (2010). The ODD protocol: A review and first update. Ecological Modelling, 221(23), 2760–2768. https://doi.org/10.1016/j.ecolmodel.2010.08.019"),
    ("[[FN12]]", "Park, J. S., O’Brien, J. C., Cai, C. J., Morris, M. R., Liang, P., & Bernstein, M. S. (2023). Generative agents: Interactive simulacra of human behavior. In Proceedings of the 36th Annual ACM Symposium on User Interface Software and Technology. https://doi.org/10.1145/3586183.3606763"),
    ("[[FN15]]", "Huang, Q., Vora, J., Liang, P., & Leskovec, J. (2024). Large language models empowered agent-based modeling and simulation: A survey and perspectives. Humanities and Social Sciences Communications, 11, 1259. https://doi.org/10.1057/s41599-024-03611-3; Wang, L., Ma, C., Feng, X., Zhang, Z., Yang, H., Zhang, J., Chen, Z., Tang, J., Chen, X., Lin, Y., Zhao, W. X., Wei, Z., & Wen, J.-R. (2024). A survey on large language model based autonomous agents. Frontiers of Computer Science, 18(6), 186345. https://doi.org/10.1007/s11704-024-40231-1"),
    ("[[FN05]]", "Ghaffarzadegan, N., Majumdar, A., Williams, R., & Hosseinichimeh, N. (2024). Generative agent-based modeling: An introduction and tutorial. System Dynamics Review, 40(1), e1761. https://doi.org/10.1002/sdr.1761; Lu, Y., Aleta, A., Du, C., Shi, L., & Moreno, Y. (2024). LLMs and generative agent-based models for complex systems research. Physics of Life Reviews, 51, 283–293. https://doi.org/10.1016/j.plrev.2024.10.013"),
    ("[[FN16]]", "Newman, M. E. J. (2004). Coauthorship networks and patterns of scientific collaboration. Proceedings of the National Academy of Sciences, 101(Suppl. 1), 5200–5205. https://doi.org/10.1073/pnas.0307545100"),
    ("[[FN14]]", "Fortunato, S., & Barthélemy, M. (2007). Resolution limit in community detection. Proceedings of the National Academy of Sciences, 104(1), 36–41. https://doi.org/10.1073/pnas.0605965104; Fortunato, S. (2010). Community detection in graphs. Physics Reports, 486(3–5), 75–174. https://doi.org/10.1016/j.physrep.2009.11.002"),
    ("[[FN17]]", "Newman, M. E. J. (2006). Finding community structure in networks using the eigenvectors of matrices. Physical Review E, 74(3), 036104. https://doi.org/10.1103/PhysRevE.74.036104"),
]

current = path
temps = []
for i, (marker, text) in enumerate(notes):
    target = path.with_name(f"agent_based_modeling_review_revised_v4_footnotes_{i+1}.docx")
    insert_note(str(current), str(target), "footnote", marker, text)
    temps.append(target)
    current = target

# Leave the final, footnoted file at the requested V4 name and clean only these
# task-created intermediate packages.
current.replace(path)
for temp in temps[:-1]:
    temp.unlink(missing_ok=True)
print(path)

