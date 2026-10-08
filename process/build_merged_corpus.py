#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Merge the new pipeline corpus with the old three-stage corpus on one taxonomy.

Standalone script. Runs directly in PyCharm (press Run) or from a terminal.

## What it does

    1. Drops the drifted multi-agent vocabulary and swarm-robotics literature
       from BOTH corpora.
    2. Applies an ABM-evidence gate to the old corpus only. The old corpus's
       llama_abm_is_abm label cannot be trusted on its own: 32% of its
       "ABM = 1" records are Autonomous Vehicles & Robotic Swarms, and 36.8%
       carry no ABM evidence at all in title + abstract.
    3. Puts the two label sets on ONE taxonomy (the old one) by mapping the new
       corpus's DeepSeek codes onto the legacy categories. See the crosswalk
       tables below - every mapping is explicit and auditable.
    4. Deduplicates by DOI then normalized title, new-pipeline rows winning.
    5. Backfills citation counts for old rows from OpenAlex.

## Why a crosswalk instead of a re-run

The new corpus is labelled with a 32-topic / 15-agent / 20-role taxonomy, the
old corpus with an 8-category / 8-domain taxonomy. Going from the finer scheme
to the coarser one is a deterministic many-to-one mapping, so it costs nothing
and is fully reproducible. Re-running an LLM over ~29k abstracts would cost a
full API pass and could only ever reproduce the same information.

The cost of the crosswalk is granularity: 32 topics collapse to 8, and the old
taxonomy has no slot for a few new codes. Those are marked "Other" or
"Unspecified" rather than forced somewhere wrong - see UNMAPPED notes below.

## Usage

    python process/build_merged_corpus.py                 # build
    python process/build_merged_corpus.py --report-only   # stats, write nothing
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

PROCESS_DIR = Path(__file__).resolve().parent
ROOT = PROCESS_DIR.parent
DB = ROOT / "database"
sys.path.insert(0, str(PROCESS_DIR))

from hard_drop import STRONG_ANCHOR_RE, AGENT_WORD_RE, SIM_WORD_RE  # noqa: E402

NEW_CORPUS = DB / "master.parquet"
OLD_CORPUS = DB / "old_corpus.parquet"
OUTPUT = DB / "corpus_legacy_taxonomy.parquet"
OUT_REPORT = PROCESS_DIR / "merge_report.md"
CITE_CACHE = DB / "_old_citations.json"

# The zero-cited pool. The old pipeline dropped every WOS record with
# times_cited == 0 before LLM screening, so those papers never received an ABM
# or topic label. The exported jsonl and its Llama-4 output are gone; what
# survives is this enrichment checkpoint, a doi -> {abstract, authors_std,
# affiliations_std} map. Titles are recovered from the old WOS backup and, for
# the rest, from OpenAlex metadata.
ZC_CHECKPOINT = Path("<ZC_CHECKPOINT_JSON>")
WOS_BACKUP = DB / "wos_backup_20260923_235908.parquet"
OA_META_CACHE = DB / "_zero_cited_openalex.json"


# --------------------------------------------------------------- filters
# Drifted multi-agent vocabulary: middleware, negotiation, web services, and
# the LLM-agent family. Same patterns as collection/WOS.py and hard_drop.py.
MULTI_AGENT_RE = re.compile(
    r"multi[- ]?agent\s+systems?|multi[- ]?agent\s+framework|"
    r"multi[- ]?agent\s+architectur|multi[- ]?agent\s+(?:reinforcement\s+)?learning|"
    r"(?:large\s+language\s+model|llm)[- ]?(?:based\s+)?agents?", re.I)

# Swarm robotics / UAV / autonomous-vehicle family.
SWARM_RE = re.compile(
    r"\bswarms?\b|\brobots?\b|\brobotics?\b|\bmulti-?robots?\b|"
    r"\buavs?\b|unmanned aerial|\bdrone[s]?\b|"
    r"autonomous (?:vehicle|car|driving|navigation)|self-driving|driverless|"
    r"\bplatoon\w*|human[- ]swarm|robocup", re.I)

# The legacy run labelled swarm-robotics work explicitly. That label is more
# reliable than the keyword patterns above, so records carrying it are removed
# even when their text happens to contain no robotics keyword.
LEGACY_ROBOTICS_TOPIC = {"Autonomous Vehicles & Robotic Swarms"}
LEGACY_ROBOTICS_DOMAIN = {"Autonomous Navigation & Swarm Coordination"}

# --------------------------------------------------------------- canonical labels
TOPIC_CATEGORIES = [
    "Urban & Transportation Planning", "Epidemiology & Public Health",
    "Economics & Financial Markets", "Social Dynamics & Human Behavior",
    "Ecology, Climate & Environmental Resources",
    "Smart Infrastructure & Energy Systems", "Other / General Domain",
]
PRIMARY_DOMAINS = [
    "Urban Mobility & Transportation", "Public Health & Disease Transmission",
    "Housing Market & Land Use Dynamics",
    "Environmental Management & Disaster Resilience",
    "Social Segregation & Opinion Formation",
    "Policy Evaluation & Disaster Response",
    "Energy Consumption & Smart Grid Infrastructure", "Unspecified",
]

# Llama-4 sometimes answered with prose instead of the requested label, e.g.
# "Cybersecurity is not listed, but closest is: Other / General Domain" or
# "Tourism and Recreation ( fits into 'Other / General Domain' ...)". Those are
# single-record tails, but they pollute any value count. Each label is snapped
# back to the canonical set: exact match, then substring match, then a keyword
# rescue, then the fallback.
_TOPIC_RESCUE = [
    ("epidemiolog", "Epidemiology & Public Health"),
    ("public health", "Epidemiology & Public Health"),
    ("healthcare", "Epidemiology & Public Health"),
    ("medical", "Epidemiology & Public Health"),
    ("ecolog", "Ecology, Climate & Environmental Resources"),
    ("environment", "Ecology, Climate & Environmental Resources"),
    ("water", "Ecology, Climate & Environmental Resources"),
    ("climate", "Ecology, Climate & Environmental Resources"),
    ("energy", "Smart Infrastructure & Energy Systems"),
    ("urban", "Urban & Transportation Planning"),
    ("transport", "Urban & Transportation Planning"),
    ("financ", "Economics & Financial Markets"),
    ("economic", "Economics & Financial Markets"),
    ("social", "Social Dynamics & Human Behavior"),
    ("education", "Social Dynamics & Human Behavior"),
]
_DOMAIN_RESCUE = [
    ("public health", "Public Health & Disease Transmission"),
    ("epidemi", "Public Health & Disease Transmission"),
    ("health", "Public Health & Disease Transmission"),
    ("environment", "Environmental Management & Disaster Resilience"),
    ("disaster", "Environmental Management & Disaster Resilience"),
    ("energy", "Energy Consumption & Smart Grid Infrastructure"),
    ("urban", "Urban Mobility & Transportation"),
    ("transport", "Urban Mobility & Transportation"),
    ("hous", "Housing Market & Land Use Dynamics"),
    ("land use", "Housing Market & Land Use Dynamics"),
    ("social", "Social Segregation & Opinion Formation"),
    ("opinion", "Social Segregation & Opinion Formation"),
    ("policy", "Policy Evaluation & Disaster Response"),
    ("supply chain", "Policy Evaluation & Disaster Response"),
    ("economic", "Policy Evaluation & Disaster Response"),
]


def canonical_label(value, allowed: list[str], rescue: list[tuple], fallback: str) -> str:
    v = str(value or "").strip()
    if v in allowed:
        return v
    low = v.lower()
    for a in allowed:
        if a.lower() in low:
            return a
    for key, target in rescue:
        if key in low:
            return target
    return fallback


# --------------------------------------------------------------- crosswalk
# New DeepSeek topic code -> legacy topic_category (8 values).
# Grounded in the legacy prompt in LLM_TOPIC.py; the economics rows are set to
# match what the legacy run itself did (see LEGACY_ECON_NOTE).
TOPIC_CATEGORY_MAP = {
    "T01": "Social Dynamics & Human Behavior",
    "T02": "Social Dynamics & Human Behavior",
    "T03": "Social Dynamics & Human Behavior",
    "T04": "Social Dynamics & Human Behavior",
    "T05": "Urban & Transportation Planning",
    "T06": "Urban & Transportation Planning",
    "T07": "Urban & Transportation Planning",
    "T08": "Urban & Transportation Planning",
    "T09": "Economics & Financial Markets",
    "T10": "Economics & Financial Markets",
    "T11": "Economics & Financial Markets",
    "T12": "Economics & Financial Markets",
    "T13": "Other / General Domain",
    "T14": "Social Dynamics & Human Behavior",
    "T15": "Epidemiology & Public Health",
    "T16": "Epidemiology & Public Health",
    "T17": "Epidemiology & Public Health",
    "T18": "Ecology, Climate & Environmental Resources",
    "T19": "Ecology, Climate & Environmental Resources",
    "T20": "Ecology, Climate & Environmental Resources",
    "T21": "Smart Infrastructure & Energy Systems",
    "T22": "Ecology, Climate & Environmental Resources",
    "T23": "Ecology, Climate & Environmental Resources",
    "T24": "Other / General Domain",
    "T25": "Other / General Domain",
    "T26": "Social Dynamics & Human Behavior",
    "T27": "Social Dynamics & Human Behavior",
    "T28": "Other / General Domain",
    "T29": "Other / General Domain",
    "T30": "Autonomous Vehicles & Robotic Swarms",
    "T31": "Other / General Domain",
    "T32": "Other / General Domain",
}

# New topic code -> legacy primary_domain (8 values).
# The legacy primary_domain list has no economics/finance category. Measured on
# the old corpus, 2,817 of its "Economics & Financial Markets" records were
# assigned "Policy Evaluation & Disaster Response", so T09-T12 follow that.
TOPIC_DOMAIN_MAP = {
    "T01": "Social Segregation & Opinion Formation",
    "T02": "Social Segregation & Opinion Formation",
    "T03": "Social Segregation & Opinion Formation",
    "T04": "Social Segregation & Opinion Formation",
    "T05": "Urban Mobility & Transportation",
    "T06": "Urban Mobility & Transportation",
    "T07": "Housing Market & Land Use Dynamics",
    "T08": "Housing Market & Land Use Dynamics",
    "T09": "Policy Evaluation & Disaster Response",
    "T10": "Policy Evaluation & Disaster Response",
    "T11": "Policy Evaluation & Disaster Response",
    "T12": "Policy Evaluation & Disaster Response",
    "T13": "Policy Evaluation & Disaster Response",
    "T14": "Social Segregation & Opinion Formation",
    "T15": "Public Health & Disease Transmission",
    "T16": "Public Health & Disease Transmission",
    "T17": "Public Health & Disease Transmission",
    "T18": "Environmental Management & Disaster Resilience",
    "T19": "Environmental Management & Disaster Resilience",
    "T20": "Environmental Management & Disaster Resilience",
    "T21": "Energy Consumption & Smart Grid Infrastructure",
    "T22": "Environmental Management & Disaster Resilience",
    "T23": "Environmental Management & Disaster Resilience",
    "T24": "Policy Evaluation & Disaster Response",
    "T25": "Policy Evaluation & Disaster Response",
    "T26": "Social Segregation & Opinion Formation",
    "T27": "Social Segregation & Opinion Formation",
    "T28": "Unspecified",
    "T29": "Unspecified",
    "T30": "Autonomous Navigation & Swarm Coordination",
    "T31": "Unspecified",
    "T32": "Unspecified",
}

# New agent code -> legacy agent_types.
AGENT_TYPE_MAP = {
    "A01": "Citizens / Individuals",
    "A02": "Citizens / Populations",
    "A03": "Citizens / Populations",
    "A04": "Citizens / Individuals",
    "A05": "Firms / Financial Traders",
    "A06": "Government / Institutional Bodies",
    "A07": "Citizens / Patients",
    "A08": "Citizens / Individuals",
    "A09": "Citizens / Individuals",
    "A10": "Biological / Pathogen Units",
    "A11": "Vehicles / Commuters",
    "A12": "Autonomous Robots / UAVs",
    "A13": "Software Agents",
    "A14": "Software Agents",
    "A15": "Software Agents",
}

# New LLM-role code -> legacy llm_role (6 values in the legacy prompt).
# L18/L19/L20 (multiple / other / unclear) have no legacy slot. They are the
# tail of the distribution - 140 rows of ~31k - and are marked "Other".
LLM_ROLE_MAP = {
    "L01": "None",
    "L02": "Agent Decision Engine",
    "L03": "Agent Decision Engine",
    "L04": "Agent Decision Engine",
    "L05": "Agent Decision Engine",
    "L06": "Natural Language Interface",
    "L07": "Agent Decision Engine",
    "L08": "Data Generation / Augmentation",
    "L09": "Data Generation / Augmentation",
    "L10": "Data Generation / Augmentation",
    "L11": "Data Generation / Augmentation",
    "L12": "Data Generation / Augmentation",
    "L13": "Content / Sentiment Analysis",
    "L14": "Content / Sentiment Analysis",
    "L15": "Code / Model Generation",
    "L16": "Code / Model Generation",
    "L17": "Data Generation / Augmentation",
    "L18": "Other",
    "L19": "Other",
    "L20": "Other",
}


def rel(p) -> str:
    try:
        return str(Path(p).relative_to(ROOT))
    except ValueError:
        return str(p)


def text_of(df: pd.DataFrame) -> pd.Series:
    return df["title"].fillna("").astype(str) + " " + df["abstract"].fillna("").astype(str)


def norm_title(s) -> pd.Series:
    return s.fillna("").astype(str).str.lower().str.replace(r"[^a-z0-9]", "", regex=True)


def norm_doi(s) -> pd.Series:
    return (s.fillna("").astype(str).str.lower().str.strip()
            .str.replace(r"^https?://doi\.org/", "", regex=True))


def abm_evidence(df: pd.DataFrame) -> pd.Series:
    """Strong anchor, or the agent+simulation pair - the same bar as stage 5."""
    t = text_of(df)
    strong = t.str.contains(STRONG_ANCHOR_RE, na=False)
    pair = (t.str.contains(AGENT_WORD_RE, na=False)
            & t.str.contains(SIM_WORD_RE, na=False))
    return strong | pair


def fetch_citations(dois: list[str]) -> dict[str, int]:
    """cited_by_count from OpenAlex, cached across runs. Metadata only - no LLM."""
    cache: dict[str, int] = {}
    if CITE_CACHE.exists():
        try:
            cache = json.load(open(CITE_CACHE, encoding="utf-8"))
        except Exception:
            cache = {}
    todo = [d for d in dois if d and d not in cache]
    for i in range(0, len(todo), 50):
        chunk = todo[i:i + 50]
        for _ in range(3):
            try:
                r = requests.get(
                    "https://api.openalex.org/works",
                    params={"filter": "doi:" + "|".join(chunk), "per-page": 50,
                            "select": "doi,cited_by_count",
                            "mailto": "abm-review@example.org"},
                    timeout=90).json()
                for w in r.get("results", []):
                    cache[w.get("doi", "").replace("https://doi.org/", "")] = \
                        w.get("cited_by_count", 0)
                break
            except Exception:
                time.sleep(3)
    if todo:
        json.dump(cache, open(CITE_CACHE, "w", encoding="utf-8"))
    return cache


def parse_authors(raw) -> list:
    try:
        v = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return []
    if not isinstance(v, list):
        return []
    out = []
    for a in v:
        if isinstance(a, dict):
            out.append({"first_name": a.get("first_name", ""),
                        "last_name": a.get("last_name", ""),
                        "position": a.get("position"),
                        "orcid": a.get("orcid", ""),
                        "institutions": a.get("institutions", [])})
    return out


EMPTY_ARR = np.array([], dtype=object)


def fetch_openalex_meta(dois: list[str]) -> dict[str, dict]:
    """Recover title / year / venue for DOIs. Metadata only - no LLM calls."""
    cache: dict[str, dict] = {}
    if OA_META_CACHE.exists():
        try:
            cache = json.load(open(OA_META_CACHE, encoding="utf-8"))
        except Exception:
            cache = {}
    todo = [d for d in dois if d and d not in cache]
    for i in range(0, len(todo), 50):
        chunk = todo[i:i + 50]
        for _ in range(3):
            try:
                r = requests.get(
                    "https://api.openalex.org/works",
                    params={"filter": "doi:" + "|".join(chunk), "per-page": 50,
                            "select": "doi,title,publication_year,primary_location",
                            "mailto": "abm-review@example.org"},
                    timeout=90).json()
                for w in r.get("results", []):
                    d = w.get("doi", "").replace("https://doi.org/", "")
                    src = (w.get("primary_location") or {}).get("source") or {}
                    cache[d] = {"title": w.get("title") or "",
                                "year": w.get("publication_year") or "",
                                "journal": src.get("display_name") or ""}
                break
            except Exception:
                time.sleep(3)
    if todo:
        json.dump(cache, open(OA_META_CACHE, "w", encoding="utf-8"))
    return cache


def load_zero_cited() -> pd.DataFrame:
    """Rebuild the zero-cited pool: checkpoint abstract + recovered title/year."""
    if not ZC_CHECKPOINT.exists():
        print(f"  zero-cited checkpoint not found: {ZC_CHECKPOINT}")
        return pd.DataFrame()
    zc = json.load(open(ZC_CHECKPOINT, encoding="utf-8")).get("doi", {})
    df = pd.DataFrame([{
        "doi": d, "abstract": (v or {}).get("abstract", ""),
        "authors_std": (v or {}).get("authors_std", ""),
        "affiliations_std": (v or {}).get("affiliations_std", ""),
    } for d, v in zc.items()])
    df["_d"] = norm_doi(df["doi"])
    print(f"  checkpoint entries: {len(df):,}")

    # titles first from the old WOS backup (no network)
    if WOS_BACKUP.exists():
        wb = pd.read_parquet(WOS_BACKUP)
        wb["_d"] = norm_doi(wb["doi"])
        wb = wb[wb["_d"].ne("")].drop_duplicates("_d").set_index("_d")
        got = df["_d"].map(wb["title"]) if "title" in wb.columns else None
        df["title"] = got.reindex(df.index) if got is not None else ""
        df["year"] = df["_d"].map(wb["publish_year"] if "publish_year" in wb.columns else wb.get("year"))
        df["journal"] = df["_d"].map(wb["journal"]) if "journal" in wb.columns else ""
        df["source"] = df["_d"].map(wb["source"]) if "source" in wb.columns else ""
    else:
        df["title"], df["year"], df["journal"], df["source"] = "", "", "", ""
    have = df["title"].fillna("").astype(str).str.strip().ne("")
    print(f"  title from WOS backup: {int(have.sum()):,}")

    # the rest from OpenAlex metadata
    meta = fetch_openalex_meta([d for d in df.loc[~have, "_d"] if d])
    for idx in df.index[~have]:
        m = meta.get(df.at[idx, "_d"])
        if m:
            df.at[idx, "title"] = m.get("title", "")
            df.at[idx, "year"] = m.get("year", "")
            df.at[idx, "journal"] = m.get("journal", "")
    df["title"] = df["title"].fillna("").astype(str)
    print(f"  title after OpenAlex   : {(df['title'].str.strip().ne('')).sum():,}")
    return df


def zero_cited_to_schema(z: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, r in z.iterrows():
        au = parse_authors(r.get("authors_std"))
        rows.append({
            "title": r.get("title", ""), "authors": "; ".join(
                f"{a['first_name']} {a['last_name']}".strip() for a in au),
            "doi": r.get("doi", ""), "abstract": r.get("abstract", ""),
            "year": str(r.get("year") or ""), "journal": r.get("journal", ""),
            "venue": r.get("journal", ""), "source": r.get("source", ""),
            "uid": "", "pmid": "", "times_cited": 0,
            "keywords": "", "mesh_terms": "", "doc_type": "",
            "authors_std": r.get("authors_std", ""),
            "affiliations_std": r.get("affiliations_std", ""),
            "venue_name": r.get("journal", ""), "doc_type_std": "",
            "year_num": pd.to_numeric(r.get("year"), errors="coerce"),
            "authors_list": au, "n_authors": len(au),
            "first_author": (f"{au[0]['first_name']} {au[0]['last_name']}".strip() if au else ""),
            "corresponding": "", "has_orcid": bool(any(a.get("orcid") for a in au)),
            "name_key": "", "countries": EMPTY_ARR, "institutions": EMPTY_ARR,
            "keywords_list": EMPTY_ARR, "mesh_list": EMPTY_ARR,
            "llm_used_num": 0, "llm_used": 0.0, "is_llm": False,
            "legacy_topic_category": "Other / General Domain",
            "legacy_primary_domain": "Unspecified",
            "legacy_agent_types": "Unspecified", "legacy_llm_role": "None",
            "legacy_simulation_scale": "Unspecified", "legacy_abm_methodology": "",
            "legacy_label_source": "unlabelled_zero_cited",
            "corpus_origin": "old_zero_cited",
        })
    return pd.DataFrame(rows)


def old_rows_to_schema(o: pd.DataFrame, citations: dict) -> pd.DataFrame:
    """Reshape legacy-corpus rows onto the master schema, keeping their labels."""
    rows = []
    for _, r in o.iterrows():
        au = parse_authors(r.get("authors_std"))
        d = norm_doi(pd.Series([r.get("doi", "")])).iloc[0]
        rows.append({
            "title": r.get("title", ""),
            "authors": "; ".join(f"{a['first_name']} {a['last_name']}".strip() for a in au),
            "doi": r.get("doi", ""), "abstract": r.get("abstract", ""),
            "year": str(r.get("year") or ""), "journal": r.get("journal", ""),
            "venue": r.get("journal", ""), "source": r.get("source", ""),
            "uid": "", "pmid": "", "times_cited": int(citations.get(d, 0)),
            "keywords": r.get("keywords", ""), "mesh_terms": r.get("mesh_terms", ""),
            "doc_type": r.get("doc_type", ""), "authors_std": r.get("authors_std", ""),
            "affiliations_std": "", "venue_name": r.get("journal", ""),
            "doc_type_std": r.get("doc_type", ""),
            "year_num": pd.to_numeric(r.get("year"), errors="coerce"),
            "authors_list": au, "n_authors": len(au),
            "first_author": (f"{au[0]['first_name']} {au[0]['last_name']}".strip() if au else ""),
            "corresponding": "", "has_orcid": bool(any(a.get("orcid") for a in au)),
            "name_key": "", "countries": EMPTY_ARR, "institutions": EMPTY_ARR,
            "keywords_list": EMPTY_ARR, "mesh_list": EMPTY_ARR,
            "llm_used_num": 0, "llm_used": 0.0,
            "is_llm": bool(str(r.get("llama_llm_has_llm")) == "1"),
            # already on the legacy taxonomy - carried over, snapped back to the
            # canonical value set so the model's prose answers do not leak in
            "legacy_topic_category": canonical_label(
                r.get("llama_topic_category"), TOPIC_CATEGORIES, _TOPIC_RESCUE,
                "Other / General Domain"),
            "legacy_primary_domain": canonical_label(
                r.get("llama_primary_domain"), PRIMARY_DOMAINS, _DOMAIN_RESCUE,
                "Unspecified"),
            "legacy_agent_types": r.get("llama_agent_types", ""),
            "legacy_llm_role": r.get("llama_llm_role", ""),
            "legacy_simulation_scale": r.get("llama_simulation_scale", ""),
            "legacy_abm_methodology": r.get("llama_abm_methodology", ""),
            "legacy_label_source": "llama4_original",
            "corpus_origin": "old_three_stage",
        })
    return pd.DataFrame(rows)


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="build_merged_corpus.py",
        description="Merge new + old corpora onto the legacy taxonomy.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--skip-zero-cited", action="store_true",
                    help="leave the zero-cited pool out of the merge")
    args = ap.parse_args()

    new = pd.read_parquet(NEW_CORPUS)
    old = pd.read_parquet(OLD_CORPUS)
    print("=" * 74)
    print("BUILD MERGED CORPUS (legacy taxonomy)")
    print("=" * 74)
    print(f"new pipeline corpus : {len(new):,}")
    print(f"old three-stage     : {len(old):,}")

    # ---- 1. filters -------------------------------------------------------
    nt = text_of(new)
    n_ma, n_sw = nt.str.contains(MULTI_AGENT_RE, na=False), nt.str.contains(SWARM_RE, na=False)
    n_robotics = new["abm_topic"].astype(str).eq("T30")
    new_keep = new[~n_ma & ~n_sw & ~n_robotics].copy()
    print(f"\nnew: - multi-agent {int(n_ma.sum()):,}  - swarm/robot {int((~n_ma & n_sw).sum()):,}"
          f"  - T30 Robotics {int((~n_ma & ~n_sw & n_robotics).sum()):,}  -> {len(new_keep):,}")

    abm = old[old["llama_abm_is_abm"].astype(str) == "1"]
    ot = text_of(abm)
    o_ma, o_sw = ot.str.contains(MULTI_AGENT_RE, na=False), ot.str.contains(SWARM_RE, na=False)
    o = abm[~o_ma & ~o_sw].copy()
    ev = abm_evidence(o)
    o = o[ev]
    # the legacy run's own robotics labels: authoritative, so also removed
    o_rob = (o["llama_topic_category"].astype(str).isin(LEGACY_ROBOTICS_TOPIC)
             | o["llama_primary_domain"].astype(str).isin(LEGACY_ROBOTICS_DOMAIN))
    print(f"old: ABM=1 {len(abm):,}  - multi-agent {int(o_ma.sum()):,}  "
          f"- swarm/robot {int((~o_ma & o_sw).sum()):,}  - no evidence {int((~ev).sum()):,}"
          f"  - legacy robotics label {int(o_rob.sum()):,}  -> {int((~o_rob).sum()):,}")
    o = o[~o_rob]

    # ---- 2. crosswalk the new corpus onto the legacy taxonomy -------------
    new_keep["legacy_topic_category"] = new_keep["abm_topic"].map(TOPIC_CATEGORY_MAP)
    new_keep["legacy_primary_domain"] = new_keep["abm_topic"].map(TOPIC_DOMAIN_MAP)
    new_keep["legacy_agent_types"] = new_keep["agent_category"].map(AGENT_TYPE_MAP)
    new_keep["legacy_llm_role"] = new_keep["llm_role"].map(LLM_ROLE_MAP)
    new_keep["legacy_simulation_scale"] = "Unspecified"
    new_keep["legacy_abm_methodology"] = ""

    # A handful of rows never got a DeepSeek label (the run covered 99.7%).
    # They stay in the corpus - they are real ABM-evidence papers - but they are
    # marked, so a topic breakdown can exclude them if wanted.
    labelled = new_keep["abm_topic"].notna()
    new_keep["legacy_label_source"] = np.where(labelled,
                                               "crosswalk_from_T/A/L",
                                               "unlabelled")
    n_unlabelled = int((~labelled).sum())
    new_keep["legacy_topic_category"] = new_keep["legacy_topic_category"].fillna("Other / General Domain")
    new_keep["legacy_primary_domain"] = new_keep["legacy_primary_domain"].fillna("Unspecified")
    new_keep["legacy_agent_types"] = new_keep["legacy_agent_types"].fillna("Unspecified")
    new_keep["legacy_llm_role"] = new_keep["legacy_llm_role"].fillna("None")

    unmapped_t = sorted({c for c in new_keep["abm_topic"].dropna()} - set(TOPIC_CATEGORY_MAP))
    unmapped_a = sorted({c for c in new_keep["agent_category"].dropna()} - set(AGENT_TYPE_MAP))
    unmapped_l = sorted({c for c in new_keep["llm_role"].dropna()} - set(LLM_ROLE_MAP))
    print(f"\ncrosswalk: unmapped topic codes {unmapped_t or 'none'}, "
          f"agent {unmapped_a or 'none'}, role {unmapped_l or 'none'}")
    print(f"rows with no DeepSeek label at all: {n_unlabelled:,}")

    # ---- 3. dedup: new corpus wins ---------------------------------------
    ntk, ndk = set(norm_title(new_keep["title"])) - {""}, set(norm_doi(new_keep["doi"])) - {""}
    dup = (norm_title(o["title"]).isin(ntk)
           | (norm_doi(o["doi"]).isin(ndk) & norm_doi(o["doi"]).ne("")))
    print(f"\nold rows already in new corpus: {int(dup.sum()):,}")
    o = o[~dup]

    if args.report_only:
        print(f"\n[report-only] merged would be {len(new_keep) + len(o):,}")
        return 0

    citations = fetch_citations([d for d in norm_doi(o["doi"]) if d])
    old_keep = old_rows_to_schema(o, citations)
    old_keep["corpus_origin"] = "old_three_stage"

    new_keep = new_keep.copy()
    new_keep["corpus_origin"] = "new_pipeline"

    # ---- 4. the zero-cited pool ------------------------------------------
    zc_keep = pd.DataFrame()
    if not args.skip_zero_cited:
        print("\nzero-cited pool:")
        z = load_zero_cited()
        if len(z):
            usable = (z["title"].str.strip().ne("") & z["abstract"].str.strip().ne(""))
            print(f"  with usable title+abstract: {int(usable.sum()):,} of {len(z):,}")
            z = z[usable].copy()
            zt = text_of(z)
            z_ma, z_sw = zt.str.contains(MULTI_AGENT_RE, na=False), zt.str.contains(SWARM_RE, na=False)
            z = z[~z_ma & ~z_sw]
            ev_z = abm_evidence(z)
            z = z[ev_z]
            print(f"  - multi-agent {int(z_ma.sum()):,}  - swarm/robot {int((~z_ma & z_sw).sum()):,}"
                  f"  - no evidence {int((~ev_z).sum()):,}  -> {len(z):,}")
            # dedup against everything already merged
            seen_t = set(norm_title(new_keep["title"])) | set(norm_title(old_keep["title"]))
            seen_d = set(norm_doi(new_keep["doi"])) | set(norm_doi(old_keep["doi"]))
            zd = norm_doi(z["doi"])
            dup_z = norm_title(z["title"]).isin(seen_t) | (zd.isin(seen_d) & zd.ne(""))
            print(f"  already in the merged corpus: {int(dup_z.sum()):,}")
            zc_keep = zero_cited_to_schema(z[~dup_z])
            print(f"  added: {len(zc_keep):,}")

    merged = pd.concat([new_keep, old_keep, zc_keep], ignore_index=True, sort=False)
    merged["times_cited_missing"] = pd.to_numeric(
        merged["times_cited"], errors="coerce").fillna(0).eq(0)
    merged["zc_exclusion"] = np.where(merged["corpus_origin"].eq("old_zero_cited"),
                                      "zero_cited", "")
    merged.to_parquet(OUTPUT, index=False)

    print(f"\nmerged: {len(merged):,} = {len(new_keep):,} new + {len(old_keep):,} old"
          f" + {len(zc_keep):,} zero-cited")
    print(f"saved -> {rel(OUTPUT)}")

    lines = [
        "# Merged corpus (legacy taxonomy)", "",
        f"- new pipeline rows: **{len(new_keep):,}**",
        f"- old three-stage rows added: **{len(old_keep):,}**",
        f"- zero-cited rows added: **{len(zc_keep):,}**",
        f"- total: **{len(merged):,}**", "",
        "## Filters applied", "",
        "| corpus | removed multi-agent | removed swarm/robot | removed no-evidence | kept |",
        "|---|---:|---:|---:|---:|",
        f"| new | {int(n_ma.sum()):,} | {int((~n_ma & n_sw).sum()):,} | 0 (stage 5) | {len(new_keep):,} |",
        f"| old | {int(o_ma.sum()):,} | {int((~o_ma & o_sw).sum()):,} | {int((~ev).sum()):,} | {len(old_keep):,} |",
        "",
        "## Legacy topic_category distribution", "",
        "| category | papers |", "|---|---:|",
    ]
    for k, v in merged["legacy_topic_category"].value_counts().items():
        lines.append(f"| {k} | {v:,} |")
    lines += ["", "## Legacy primary_domain distribution", "",
              "| domain | papers |", "|---|---:|"]
    for k, v in merged["legacy_primary_domain"].value_counts().items():
        lines.append(f"| {k} | {v:,} |")
    lines += ["", "## Notes", "",
              "- `legacy_label_source` records where each label came from: "
              "`llama4_original` (old corpus), `crosswalk_from_T/A/L` (new corpus), "
              "`unlabelled_zero_cited` (never LLM-screened).",
              "- `zc_exclusion = \"zero_cited\"` marks the pool the old pipeline had "
              "dropped for having no citations; all of them carry `times_cited = 0` "
              "by construction, so exclude them from citation-weighted metrics."]
    OUT_REPORT.write_text("\n".join(lines), encoding="utf-8")
    print(f"saved -> {rel(OUT_REPORT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
