#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage 5: keep only records that show ABM evidence, and drop known false positives.

Standalone script. Runs directly in PyCharm (press Run) or from a terminal.

## What it does

Three rules are applied together; a record is removed when any one fires.

    Rule A - ambiguous keyword, no ABM evidence
        The collection query ORs 17 simulation-software names. Five of them are
        also ordinary words, names or product names, so they pull in records that
        have nothing to do with agent-based modelling. Measured by title match,
        cross-checked against ABM vocabulary:

            keyword   title hits   with ABM evidence   pure noise
            mesa           900             4               896
            mason          601             6               595
            jade           342            16               326
            gama           151             8               143
            jason          150             5               145

        Examples: "Experience with Processes and Monitors in Mesa" (the Mesa
        programming language), "Mason: A Global Floorplanning Approach for VLSI
        Design", "Total body gama-ray counting of Pu-239 in beagles" (gama is a
        misspelling of gamma).

    Rule B - no ABM evidence at all
        A record must show some ABM evidence to stay. Evidence is either a strong
        anchor term, or the agent+simulation pair test described below.
        Measured on the current corpus, the records with no evidence are things
        like "Anisotropic oxidation of circular mesas for photonic devices",
        "Sleep and musculoskeletal pain" and "Molecular epidemiology of
        foot-and-mouth disease virus" - unrelated to ABM.

    Rule C - excluded vocabulary
        Some phrases have drifted away from agent-based modelling and must not
        appear in the corpus at all, whatever else the record says:

            multi-agent system / multiagent system / multi agent system
            multi-agent framework / multi-agent architecture
            multi-agent learning / multi-agent reinforcement learning
            large language model agent / LLM agent / LLM-based agent

        Multi-agent *systems* research is middleware, negotiation and web
        services; the LLM-agent family is a different literature again. Neither
        is agent-based modelling.

        collection/WOS.py already excludes these phrases in the query itself.
        This rule is the safety net for records that arrive from PubMed or DBLP
        (whose queries are separate) or from a collection run made before the
        exclusion was added. They are hard-dropped even when the record also
        contains a strong anchor, so the audit CSV is the full list of what was
        taken out and why.

## ABM evidence

Strong anchors - any one is sufficient:

    agent-based / individual-based / agent-based / agent-oriented / agentbased
    ABM / ABMs
    netlogo / repast / anylogic / cormas / starlogo / agentpy / agents.jl /
        agentscript / simudyne
    microsimulation / activity-based / cellular automata
    social simulation / artificial society / complex adaptive system /
        generative social science

Pair test - a generic agent word together with simulation or modelling
vocabulary:

    (agent | agents | multi-agent | multiagent)  AND  (simulat | model | modelling)

The pair test exists because a bare agent word is not enough on its own, while
"multi-agent simulation of predator-prey dynamics" clearly is ABM. Requiring
both halves keeps the second and rejects the first. It alone rescues ~300
papers that never write "agent-based".

Note that rule C removes the other half of that family - "multi-agent system",
"multi-agent framework", "multi-agent learning" and the LLM agents - before the
evidence test is even consulted. "Multi-agent simulation" and "multi-agent
model" stay: they are genuine ABM vocabulary.

## Thresholds

The rules are intentionally permissive: a false negative deletes a genuine ABM
paper, which is worse than keeping one marginal record. Tune with:

    --min-evidence-hits   how many anchor occurrences a record needs (default 1)
    --no-exclusions       switch rule C off (keep the drifted vocabulary)
    --keep-journals       never drop these venues, whatever the rules say
    --drop-journals       always drop these venues

## Outputs

    database/analysis_literature.parquet     rewritten in place (.bak kept)
    process/stage5_hard_drop.csv             every removed record, with its rule
    process/stage5_excluded_terms.csv        the rule C removals on their own
    process/stage_summary.csv                appended with this stage's rows

## Usage

    python process/hard_drop.py                     # apply all three rules (default)
    python process/hard_drop.py --report-only       # count only, change nothing
    python process/hard_drop.py --rule-a-only       # only the false-positive rule
    python process/hard_drop.py --rule-b-only       # only the evidence requirement
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

import pandas as pd

PROCESS_DIR = Path(__file__).resolve().parent
ROOT = PROCESS_DIR.parent
DB = ROOT / "database"

SOURCE = DB / "analysis_literature.parquet"

OUT_DROPPED = PROCESS_DIR / "stage5_hard_drop.csv"
OUT_EXCLUDED = PROCESS_DIR / "stage5_excluded_terms.csv"
OUT_SUMMARY = PROCESS_DIR / "stage_summary.csv"


# ============================================================ ABM evidence
STRONG_ANCHOR_RE = re.compile(
    # core forms and spelling variants
    r"agent[- ]based|individual[- ]based|agent[- ]base|agent[- ]oriented|agentbased|"
    # abbreviation
    r"\bABM\b|\bABMs\b|"
    # named toolkits
    r"netlogo|repast|anylogic|cormas|starlogo|agentpy|agents\.jl|agentscript|simudyne|"
    # sibling and synonymous methods
    r"microsimulat|activity[- ]based|cellular automat|"
    r"social simulation|social simulat|"
    r"generative social science|artificial societ|complex adaptive system",
    re.I,
)

AGENT_WORD_RE = re.compile(r"\bagent\b|\bagents\b|multi[- ]?agent|multiagent", re.I)
SIM_WORD_RE = re.compile(r"simulat|model|modelling|modeling|microsimulat", re.I)


# ============================================================ false positives
# Ambiguous keyword -> reason recorded in the audit CSV.
FALSE_POSITIVE_PATTERNS: dict[str, str] = {
    r"\bmesa\b": "Mesa programming language / mesa laser structure / place name",
    r"\bmason\b": "surname Mason / mason bee / place name",
    r"\bgama\b": "GAMA galaxy survey / misspelling of gamma",
    r"\bjason\b": "given name Jason",
    r"\bjade\b": "material jade / journal name",
    r"\bspade\b": "common noun spade / SPADE algorithm",
    r"\bsarl\b": "French company form SARL",
    r"\bmatsim\b": "MATSim transport toolkit, no ABM evidence",
}

# Venues never dropped, or always dropped. Empty by default: filter_venues.py
# already handles journals by ABM share, which is more defensible than a
# hand-written list. Use these for manual overrides.
KEEP_JOURNALS: set[str] = set()
DROP_JOURNALS: set[str] = set()


# ============================================================ excluded vocabulary
# Phrases that no longer mean agent-based modelling, hard-dropped whatever else
# the record contains. Mirrors EXCLUDE_TERMS in collection/WOS.py, which keeps
# them out at query time; this is the safety net for PubMed, DBLP and for any
# collection run made before the exclusion existed.
#
# "multi-agent simulation" and "multi-agent model" are deliberately absent from
# this table: they are genuine ABM vocabulary and stay in the corpus.
EXCLUDED_PATTERNS: dict[str, str] = {
    # "multi[- ]?agent" already covers "multiagent", so no separate entry is needed
    r"multi[- ]?agent\s+systems?": "multi-agent systems (middleware / negotiation), not ABM",
    r"multi[- ]?agent\s+framework": "multi-agent framework, not ABM",
    r"multi[- ]?agent\s+architectur": "multi-agent architecture, not ABM",
    r"multi[- ]?agent\s+(?:reinforcement\s+)?learning": "multi-agent learning / MARL, not ABM",
    r"(?:large\s+language\s+model|llm)[- ]?(?:based\s+)?agents?": "LLM agent, not ABM",
}

AUDIT_COLS = ["title", "authors", "year", "journal", "venue", "doi", "source",
              "doc_type", "times_cited"]


def norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def rel(p: Path) -> str:
    try:
        return str(Path(p).relative_to(ROOT))
    except ValueError:
        return str(p)


def abm_evidence(df: pd.DataFrame, min_hits: int = 1) -> pd.Series:
    """True where a record shows ABM evidence (strong anchor or pair test).

    With min_hits > 1 a record must contain at least that many anchor
    occurrences, which is a stricter bar for borderline corpora.
    """
    text = (df["title"].fillna("").astype(str) + " " + df["abstract"].fillna("").astype(str))
    if min_hits <= 1:
        strong = text.str.contains(STRONG_ANCHOR_RE, na=False)
    else:
        strong = text.str.count(STRONG_ANCHOR_RE) >= min_hits
    pair = (text.str.contains(AGENT_WORD_RE, na=False)
            & text.str.contains(SIM_WORD_RE, na=False))
    return strong | pair


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="hard_drop.py",
        description="Keep only records with ABM evidence and drop known false positives.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--file", default=str(SOURCE))
    ap.add_argument("--out", default="", help="default: overwrite --file")
    ap.add_argument("--report-only", action="store_true",
                    help="count the matches and write the audit CSV, but change no data")
    ap.add_argument("--rule-a-only", action="store_true",
                    help="apply only the ambiguous-keyword rule")
    ap.add_argument("--rule-b-only", action="store_true",
                    help="apply only the ABM-evidence requirement")
    ap.add_argument("--no-exclusions", action="store_true",
                    help="switch rule C off and keep the drifted vocabulary "
                         "(multi-agent systems, LLM agents) in the corpus")
    ap.add_argument("--min-evidence-hits", type=int, default=1,
                    help="anchor occurrences a record needs to count as evidence "
                         "(default 1; raise to tighten)")
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()

    src = Path(args.file)
    if not src.exists():
        print(f"[error] {src} not found")
        return 1
    out_path = Path(args.out) if args.out else src

    df = pd.read_parquet(src)
    n0 = len(df)
    print("=" * 72)
    print("STAGE 5 - keep ABM-evidence records, drop false positives")
    print("=" * 72)
    print(f"  input: {n0:,} rows")

    text = df["title"].fillna("").astype(str) + " " + df["abstract"].fillna("").astype(str)
    strong = text.str.contains(STRONG_ANCHOR_RE, na=False)
    pair = (text.str.contains(AGENT_WORD_RE, na=False)
            & text.str.contains(SIM_WORD_RE, na=False))
    evidence = abm_evidence(df, args.min_evidence_hits)

    print(f"\n  ABM evidence:")
    print(f"    strong anchor terms       {int(strong.sum()):>8,} "
          f"({strong.mean()*100:5.1f}%)")
    print(f"    agent + simulation pair   {int(pair.sum()):>8,} "
          f"({pair.mean()*100:5.1f}%)")
    print(f"    either (kept)             {int(evidence.sum()):>8,} "
          f"({evidence.mean()*100:5.1f}%)")
    if args.min_evidence_hits > 1:
        print(f"    (min-evidence-hits = {args.min_evidence_hits})")

    drop = pd.Series(False, index=df.index)
    reason = pd.Series("", index=df.index)

    # ---- rule A: ambiguous keyword with no ABM evidence ----
    if not args.rule_b_only:
        print(f"\n  rule A - ambiguous keyword without ABM evidence:")
        for pat, why in FALSE_POSITIVE_PATTERNS.items():
            hit = text.str.contains(re.compile(pat, re.I), na=False)
            rem = hit & ~evidence
            if int(hit.sum()):
                print(f"    {pat[2:-2]:<8} matched {int(hit.sum()):>6,}  "
                      f"removed {int(rem.sum()):>6,}  ({why[:46]})")
            drop |= rem
            reason = reason.mask(rem & reason.eq(""), f"rule A: {why}")

    # ---- rule B: no ABM evidence ----
    if not args.rule_a_only:
        rem_b = ~evidence
        print(f"\n  rule B - no ABM evidence: removed {int(rem_b.sum()):,}")
        drop |= rem_b
        reason = reason.mask(rem_b & reason.eq(""),
                             "rule B: no ABM anchor or agent+simulation evidence")

    # ---- rule C: vocabulary that no longer means agent-based modelling ----
    # Applied last but given priority over A and B in the audit CSV, because
    # "this phrase is out of scope" is the more useful explanation than
    # "no evidence found".
    excl = pd.Series(False, index=df.index)
    if not args.no_exclusions:
        print(f"\n  rule C - excluded vocabulary (hard drop, evidence ignored):")
        for pat, why in EXCLUDED_PATTERNS.items():
            hit = text.str.contains(re.compile(pat, re.I), na=False)
            if int(hit.sum()):
                print(f"    {why[:48]:<50} {int(hit.sum()):>6,}")
            excl |= hit
        print(f"    {'total':<50} {int(excl.sum()):>6,}")
        drop |= excl
        # per-pattern reason, overriding whatever rules A and B recorded
        for pat, why in EXCLUDED_PATTERNS.items():
            hit = text.str.contains(re.compile(pat, re.I), na=False)
            reason = reason.mask(hit, f"rule C: {why}")

    # ---- manual overrides ----
    j = df["journal"].fillna("").astype(str).map(norm)
    v = df["venue"].fillna("").astype(str).map(norm)
    if DROP_JOURNALS:
        h = j.isin(DROP_JOURNALS) | v.isin(DROP_JOURNALS)
        print(f"\n  manual: drop-journals removed {int(h.sum()):,}")
        drop |= h
        reason = reason.mask(h & reason.eq(""), "manual: journal on drop list")
    if KEEP_JOURNALS:
        h = j.isin(KEEP_JOURNALS) | v.isin(KEEP_JOURNALS)
        print(f"  manual: keep-journals rescued {int((h & drop).sum()):,}")
        drop &= ~h
        reason = reason.mask(h, "")

    n_drop = int(drop.sum())
    print("\n" + "-" * 62)
    print(f"  {'Removed':<32}{-n_drop:>10,}   remaining {n0-n_drop:>9,}"
          f"  ({-n_drop/n0*100:5.1f}%)")

    dropped = df[drop].copy()
    if n_drop:
        dropped["drop_reason"] = reason[drop]
        cols = [c for c in AUDIT_COLS if c in dropped.columns]
        dropped[cols + ["drop_reason"]].to_csv(
            OUT_DROPPED, index=False, encoding="utf-8-sig")
        print(f"\n  removed records written -> {OUT_DROPPED.name}  ({n_drop:,} rows)")

    n_excl = int(excl.sum())
    if n_excl:
        excluded = df[excl].copy()
        excluded["drop_reason"] = reason[excl]
        cols = [c for c in AUDIT_COLS if c in excluded.columns]
        excluded[cols + ["drop_reason"]].to_csv(
            OUT_EXCLUDED, index=False, encoding="utf-8-sig")
        print(f"  excluded vocabulary     -> {OUT_EXCLUDED.name}  ({n_excl:,} rows)")

    rule_tag = ("rule A only" if args.rule_a_only else
                "rule B only" if args.rule_b_only else "rules A + B")
    if not args.no_exclusions:
        rule_tag += " + C"
    rows = [{
        "stage": "Stage 5 - ABM evidence", "step": "Keep ABM-evidence records",
        "removed": n_drop, "remaining": n0 - n_drop,
        "pct_removed": round(n_drop / n0 * 100, 2),
        "csv": OUT_DROPPED.name if n_drop else "",
        "note": f"{rule_tag}; min_evidence_hits={args.min_evidence_hits}; "
                f"excluded_vocabulary={n_excl}",
    }]

    if not args.no_exclusions:
        # same stage, second audit file: record it so the ledger stays complete
        rows.append({
            "stage": "Stage 5 - ABM evidence", "step": "Excluded vocabulary (rule C)",
            "removed": n_excl, "remaining": n0 - n_drop,
            "pct_removed": round(n_excl / n0 * 100, 2),
            "csv": OUT_EXCLUDED.name if n_excl else "",
            "note": "multi-agent systems / framework / learning / LLM agents",
        })
    if OUT_SUMMARY.exists():
        prev = pd.read_csv(OUT_SUMMARY)
        # drop rows this same stage wrote on an earlier run, so re-runs do not stack
        prev = prev[prev["stage"] != "Stage 5 - ABM evidence"]
        rows = prev.to_dict("records") + rows
    pd.DataFrame(rows).to_csv(OUT_SUMMARY, index=False, encoding="utf-8-sig")
    print(f"  stage summary -> {OUT_SUMMARY.name}")

    if args.report_only:
        print("\n  [report-only] no data written.")
        return 0

    kept = df[~drop].reset_index(drop=True)
    if not args.no_backup and out_path == src:
        bak = Path(str(src) + ".bak")
        if not bak.exists():
            shutil.copy2(src, bak)
            print(f"  backup created: {bak.name}")
    kept.to_parquet(out_path, index=False)
    print(f"\n  saved -> {rel(out_path)}  ({len(kept):,} rows)")

    # final composition
    ktext = (kept["title"].fillna("").astype(str) + " "
             + kept["abstract"].fillna("").astype(str))
    ks = ktext.str.contains(STRONG_ANCHOR_RE, na=False)
    kp = (ktext.str.contains(AGENT_WORD_RE, na=False)
          & ktext.str.contains(SIM_WORD_RE, na=False))
    print(f"  kept: strong anchor {int(ks.sum()):,} | pair test {int(kp.sum()):,} | "
          f"both {int((ks & kp).sum()):,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
