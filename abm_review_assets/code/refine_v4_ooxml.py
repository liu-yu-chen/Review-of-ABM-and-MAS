from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
from lxml import etree
import os

path = Path(r"D:\ONE DRIVE_PERSONAL\OneDrive\文档\agent_based_modeling_review_revised_v4.docx")
tmp = path.with_name("agent_based_modeling_review_revised_v4_refined.tmp.docx")
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W}
qn = lambda local: f"{{{W}}}{local}"

with ZipFile(path, "r") as zin:
    document = etree.fromstring(zin.read("word/document.xml"))
    body = document.find("w:body", NS)

    def ptext(p):
        return "".join(p.xpath(".//w:t/text()", namespaces=NS))

    # Correct the incomplete journal metadata and alphabetize the bibliography.
    refs_heading = next(p for p in body.findall("w:p", NS) if ptext(p).strip() == "References")
    ref_nodes = [p for p in body.findall("w:p", NS) if p.getprevious() is not None and ptext(p).startswith((
        "Bonabeau,", "Fortunato,", "Ghaffarzadegan,", "Gilbert,", "Grimm,", "Huang,", "Lu,", "Macal,", "Newman,", "Park,", "Priem,", "Railsback,", "Wang,"
    ))]
    old_ghaff = next(p for p in ref_nodes if ptext(p).startswith("Ghaffarzadegan,"))
    new_ghaff = ("Ghaffarzadegan, N., Majumdar, A., Williams, R., & Hosseinichimeh, N. (2024). Generative agent-based modeling: "
                 "An introduction and tutorial. System Dynamics Review, 40(1), e1761. https://doi.org/10.1002/sdr.1761")
    gh_runs = old_ghaff.xpath(".//w:t", namespaces=NS)
    gh_runs[0].text = new_ghaff
    for t in gh_runs[1:]:
        t.text = ""

    # Retain the author's source set and order it consistently by first author.
    surname_order = ["Bonabeau,", "Fortunato, S. (", "Fortunato, S., &", "Ghaffarzadegan,", "Gilbert,", "Grimm, V., Berger, U., Bastiansen", "Grimm, V., Berger, U., DeAngelis", "Huang,", "Lu,", "Macal,", "Newman, M. E. J. (2004)", "Newman, M. E. J. (2006)", "Park,", "Priem,", "Railsback,", "Wang,"]
    ordered = []
    for key in surname_order:
        found = [p for p in ref_nodes if ptext(p).startswith(key)]
        if len(found) != 1:
            raise ValueError(f"Reference key {key!r}: found {len(found)}")
        ordered.extend(found)
    for p in ref_nodes:
        p.getparent().remove(p)
    anchor = refs_heading
    for p in ordered:
        anchor.addnext(p)
        anchor = p

    # Prevent existing table rows from splitting across pages.
    for tr in body.xpath(".//w:tr", namespaces=NS):
        trpr = tr.find("w:trPr", NS)
        if trpr is None:
            trpr = etree.Element(qn("trPr"))
            tr.insert(0, trpr)
        if trpr.find("w:cantSplit", NS) is None:
            trpr.append(etree.Element(qn("cantSplit")))

    footnotes = etree.fromstring(zin.read("word/footnotes.xml"))
    for note in footnotes.findall("w:footnote", NS):
        note_id = note.get(qn("id"))
        if note_id in ("-1", "0"):
            continue
        p = note.find("w:p", NS)
        ppr = p.find("w:pPr", NS)
        if ppr is None:
            ppr = etree.Element(qn("pPr"))
            p.insert(0, ppr)
        spacing = ppr.find("w:spacing", NS)
        if spacing is None:
            spacing = etree.SubElement(ppr, qn("spacing"))
        spacing.set(qn("after"), "0")
        spacing.set(qn("line"), "220")
        spacing.set(qn("lineRule"), "auto")
        indent = ppr.find("w:ind", NS)
        if indent is None:
            indent = etree.SubElement(ppr, qn("ind"))
        indent.set(qn("left"), "360")
        indent.set(qn("hanging"), "360")
        runs = p.findall("w:r", NS)
        if len(runs) >= 2:
            ref_rpr = runs[0].find("w:rPr", NS)
            if ref_rpr is None:
                ref_rpr = etree.Element(qn("rPr"))
                runs[0].insert(0, ref_rpr)
            vert = ref_rpr.find("w:vertAlign", NS)
            if vert is None:
                vert = etree.SubElement(ref_rpr, qn("vertAlign"))
            vert.set(qn("val"), "superscript")
            size = ref_rpr.find("w:sz", NS)
            if size is None:
                size = etree.SubElement(ref_rpr, qn("sz"))
            size.set(qn("val"), "16")
            for r in runs[1:]:
                rpr = r.find("w:rPr", NS)
                if rpr is None:
                    rpr = etree.Element(qn("rPr"))
                    r.insert(0, rpr)
                for tag in ("sz", "szCs"):
                    elem = rpr.find(f"w:{tag}", NS)
                    if elem is None:
                        elem = etree.SubElement(rpr, qn(tag))
                    elem.set(qn("val"), "18")
                for t in r.findall("w:t", NS):
                    if t.text and t.text.startswith(" "):
                        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")

    replacements = {
        "word/document.xml": etree.tostring(document, xml_declaration=True, encoding="UTF-8", standalone=True),
        "word/footnotes.xml": etree.tostring(footnotes, xml_declaration=True, encoding="UTF-8", standalone=True),
    }
    with ZipFile(tmp, "w", ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            zout.writestr(item, replacements.get(item.filename, zin.read(item.filename)))

os.replace(tmp, path)
print(path)
