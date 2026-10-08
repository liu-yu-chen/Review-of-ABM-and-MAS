#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Literature pipeline: merge -> enrich -> clean, with per-stage CSV audit trails.

Standalone script. Runs directly in PyCharm (no command-line arguments needed)
or from a terminal.

## What it does

    1. merge    Combine the raw source tables in database/ and de-duplicate
    2. enrich   Fill abstract / authors_std / affiliations_std from OpenAlex
    3. clean    Drop non-English titles, editorial/letter/review types,
                and records without an abstract
    4. run      Execute the three stages above in order

## Data flow

    database/wos.parquet            ┐
    database/pubmed.parquet         ├─ merge  ─► database/merged_literature.parquet
    database/dblp_agent.parquet     ┘              │
                                                   ├─ enrich ─► (writes 3 columns back in place)
                                                   │
                                                   └─ clean  ─► database/filtered_literature.parquet

## Audit trail

Every stage that removes records writes a CSV listing what was removed, so the
exclusions can be inspected individually rather than only counted:

    process/stage1_merge_removed.csv         duplicates dropped by merge
    process/stage2_enrich_missing.csv        records still lacking author fields
    process/stage3_clean_non_english.csv     non-English titles
    process/stage3_clean_doc_type.csv        editorial / letter / review types
    process/stage3_clean_no_abstract.csv     records without an abstract
    process/stage3_clean_zero_cited.csv      zero-cited records (optional)
    process/stage_summary.csv                one row per stage: removed / remaining

The run summary is also appended to database/pipeline_report.md.

## Clean-stage scope

By default the three filters apply to **all sources**. The legacy behaviour
(only WOS rows) is available via --wos-only.

Note on differing criteria versus the old filter_literature.py:

    | Criterion     | old script          | this script            |
    |---------------|---------------------|------------------------|
    | Scope         | WOS rows only       | all sources (--wos-only to revert)
    | Non-English   | >= 20% non-Latin    | same
    | Doc types     | WOS doc_type only   | WOS doc_type + DBLP type
    | Reviews       | kept                | dropped
    | Zero-cited    | dropped             | kept (--drop-zero-cited to drop)

## Usage

    python process/pipeline.py                # run all three stages (default)
    python process/pipeline.py merge
    python process/pipeline.py enrich
    python process/pipeline.py clean
    python process/pipeline.py clean --wos-only
    python process/pipeline.py enrich --limit 100     # trial run
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------- paths
# Resolved from this file's location, so the script works both when run from
# PyCharm (any working directory) and from a terminal.
PROCESS_DIR = Path(__file__).resolve().parent
ROOT = PROCESS_DIR.parent
DB = ROOT / "database"

sys.path.insert(0, str(PROCESS_DIR))
sys.stdout.reconfigure(encoding="utf-8")

# Reuse the OpenAlex client and parsers from enrich.py so the field schema
# cannot drift between the two scripts.
from enrich import (  # noqa: E402
    BATCH_DOI, CHECKPOINTS, Ckpt, lookup_dois_batch, make_client,
    norm_title, normalize_doi, write_back,
)

# One definition of "this looks like agent-based modelling", shared with stage 5
# and with the corpus merge, so the three can never disagree.
from hard_drop import STRONG_ANCHOR_RE  # noqa: E402

SOURCES = {
    "wos": DB / "wos.parquet",
    "pubmed": DB / "pubmed.parquet",
    "dblp": DB / "dblp_agent.parquet",
}
MERGED = DB / "merged_literature.parquet"
FILTERED = DB / "filtered_literature.parquet"
REPORT = DB / "pipeline_report.md"

# CSV audit trails (written under process/)
OUT_MERGE = PROCESS_DIR / "stage1_merge_removed.csv"
OUT_ENRICH_MISSING = PROCESS_DIR / "stage2_enrich_missing.csv"
OUT_CLEAN_NON_EN = PROCESS_DIR / "stage3_clean_non_english.csv"
OUT_CLEAN_TYPE = PROCESS_DIR / "stage3_clean_doc_type.csv"
OUT_CLEAN_NOABS = PROCESS_DIR / "stage3_clean_no_abstract.csv"
OUT_CLEAN_KEPT_NOABS = PROCESS_DIR / "stage3_kept_no_abstract.csv"
OUT_CLEAN_ZERO = PROCESS_DIR / "stage3_clean_zero_cited.csv"
OUT_SUMMARY = PROCESS_DIR / "stage_summary.csv"

# ---------------------------------------------------------------- rules
# Document types to drop. Applies to the WOS `doc_type` column and the DBLP
# `type` column. Matching is on comma/semicolon-separated tokens, so
# "Article; Review" matches "review" while "Journal Article" does not.
DROP_TYPES = {
    # editorial / comment material
    "editorial", "editorial material", "comment", "commentary", "response",
    "reply", "letter", "note", "notes", "correction", "erratum",
    # review material
    "review", "review-article", "systematic review", "meta-analysis",
    "book review", "book-review", "survey",
    # short communications
    "short communication", "short survey", "abstract", "proceedings paper",
    "communication",
}

# Non-Latin writing systems. Greek is excluded on purpose: Delta/Sigma appear
# as mathematical symbols in otherwise English titles.
NON_LATIN_RE = re.compile(
    r"[\u4E00-\u9FFF\u3400-\u4DBF"      # CJK ideographs
    r"\u3040-\u30FF"                    # hiragana + katakana
    r"\uAC00-\uD7AF"                    # hangul
    r"\u0400-\u04FF"                    # cyrillic
    r"\u0600-\u06FF\u0750-\u077F"       # arabic
    r"\u0590-\u05FF"                    # hebrew
    r"\u0E00-\u0E7F"                    # thai
    r"\u0900-\u097F"                    # devanagari
    r"\u0E80-\u0EFF]"                   # lao
)
LATIN_RE = re.compile(r"[A-Za-z]")
NON_EN_THRESHOLD = 0.20

# Columns kept in the audit CSVs, so the files stay readable.
AUDIT_COLS = ["title", "authors", "year", "journal", "venue", "doi",
              "source", "doc_type", "times_cited"]


# ================================================================ ledger
class Ledger:
    """Per-stage audit: records how many rows each step removed and what is left.

    `context` is an optional hook so a running stage can append its own rows to
    process/stage_summary.csv as soon as the stage finishes, without the caller
    having to collect the ledger objects.
    """

    def __init__(self, stage: str, total: int):
        self.stage = stage
        self.total = total
        self.rows: list[dict] = []

    def step(self, label: str, removed: int, remaining: int,
             csv: Path | None = None, note: str = "") -> None:
        pct = (removed / self.total * 100) if self.total else 0.0
        self.rows.append({
            "stage": self.stage, "step": label, "removed": int(removed),
            "remaining": int(remaining), "pct_removed": round(pct, 2),
            "csv": csv.name if csv else "", "note": note,
        })
        tag = f"  -> {csv.name}" if csv else ""
        print(f"  {label:<32}{-int(removed):>10,}   remaining {remaining:>9,}"
              f"  ({-pct:5.1f}%){tag}")

    def finish(self) -> None:
        last = self.rows[-1]["remaining"] if self.rows else self.total
        removed = self.total - last
        pct = (removed / self.total * 100) if self.total else 0.0
        print("  " + "-" * 62)
        print(f"  {'TOTAL removed':<32}{-removed:>10,}   remaining {last:>9,}"
              f"  ({-pct:5.1f}%)")

    def to_markdown(self) -> str:
        lines = [f"## {self.stage}", "",
                 f"Starting records: **{self.total:,}**", "",
                 "| Step | Removed | Remaining | % removed | Audit CSV | Note |",
                 "|---|---:|---:|---:|---|---|"]
        for r in self.rows:
            lines.append(f"| {r['step']} | -{r['removed']:,} | {r['remaining']:,} "
                         f"| {r['pct_removed']:.1f}% | {r['csv']} | {r['note']} |")
        if self.rows:
            tot = self.total - self.rows[-1]["remaining"]
            lines.append(f"| **TOTAL** | **-{tot:,}** | "
                         f"**{self.rows[-1]['remaining']:,}** "
                         f"| **{tot/self.total*100:.1f}%** | | |")
        return "\n".join(lines) + "\n"

    def write_summary(self) -> None:
        """Append this stage's rows to process/stage_summary.csv."""
        flush_summary(self.rows)


# Summary rows from every stage of this run, accumulated in-process so the
# combined CSV is rewritten in stage order rather than only the last stage.
_SUMMARY_ROWS: list[dict] = []


def flush_summary(rows: list[dict]) -> None:
    """Append `rows` to the running summary and rewrite process/stage_summary.csv."""
    _SUMMARY_ROWS.extend(rows)
    if not _SUMMARY_ROWS:
        return
    pd.DataFrame(_SUMMARY_ROWS).to_csv(OUT_SUMMARY, index=False, encoding="utf-8-sig")
    print(f"  stage summary -> {OUT_SUMMARY.name}  "
          f"({len(_SUMMARY_ROWS)} rows total)")


def append_report(section: str) -> None:
    header = "# Literature pipeline report\n\n"
    old = REPORT.read_text(encoding="utf-8") if REPORT.exists() else header
    REPORT.write_text(old + "\n" + section, encoding="utf-8")
    print(f"  report appended -> {rel(REPORT)}")


def dump_removed(df: pd.DataFrame, path: Path, cols: list[str] | None = None) -> Path | None:
    """Write the removed rows to CSV. Returns the path, or None when empty."""
    if len(df) == 0:
        return None
    cols = [c for c in (cols or AUDIT_COLS) if c in df.columns]
    out = df[cols].copy()
    out.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"    removed rows written -> {path.name}  ({len(out):,} rows)")
    return path



def rel(path: Path) -> str:
    """Path relative to the project root when possible, else the absolute path.

    Avoids ValueError when a caller (e.g. a test) points an output somewhere
    outside the repository.
    """
    try:
        return str(Path(path).relative_to(ROOT))
    except ValueError:
        return str(path)

# ================================================================ helpers
def parse_authors(raw) -> list:
    """Normalize the author field of all three sources into a list of names."""
    if raw is None:
        return []
    if isinstance(raw, list) or hasattr(raw, "tolist"):
        try:
            return [str(a) for a in list(raw) if a]
        except TypeError:
            pass
    if isinstance(raw, str):
        s = raw.strip()
        if s.startswith("["):                 # PubMed stores a JSON array
            try:
                return [str(a) for a in json.loads(s) if a]
            except Exception:
                pass
        return [a.strip() for a in s.split(";") if a.strip()]
    return []


def authors_surnames(raw) -> set:
    out = set()
    for name in parse_authors(raw):
        name = name.strip()
        if not name:
            continue
        if "," in name:                       # "Last, First"
            out.add(name.split(",")[0].strip().lower())
        else:                                 # "First Last"
            toks = name.split()
            if toks:
                out.add(toks[-1].lower())
    return out


def join_authors(raw) -> str:
    names = []
    for a in parse_authors(raw):
        if a not in names:
            names.append(a)
    return "; ".join(names)


def first_nonempty(series):
    for v in series:
        if isinstance(v, str) and v.strip():
            return v
        if v is not None and not isinstance(v, str):
            return v
    return ""


def merge_unique(series, sep=";"):
    out = []
    for v in series:
        if not isinstance(v, str) or not v.strip():
            continue
        for part in v.split(sep):
            part = part.strip()
            if part and part not in out:
                out.append(part)
    return sep.join(out)


MERGE_COLS = ["title", "authors", "doi", "abstract", "year", "journal", "venue",
              "source", "uid", "pmid", "keywords", "mesh_terms"]
UNIQUE_COLS = ["source", "uid", "pmid", "keywords", "mesh_terms"]


def load_normalized(name: str, path: Path) -> pd.DataFrame:
    """Read one source table and map its columns onto the shared schema."""
    df = pd.read_parquet(path)
    print(f"  {name:<8}{len(df):>9,} rows")

    out = pd.DataFrame(index=df.index)
    out["title"] = df["title"].fillna("").astype(str)
    out["authors"] = df["authors"].apply(join_authors)
    out["doi"] = df["doi"].apply(normalize_doi)
    out["abstract"] = (df["abstract"].fillna("").astype(str)
                       if "abstract" in df.columns else "")

    if name == "wos":
        out["year"] = df["publish_year"].fillna("").astype(str)
    elif "year" in df.columns:
        out["year"] = df["year"].fillna("").astype(str)
    else:
        out["year"] = ""

    out["journal"] = df["journal"].fillna("") if "journal" in df.columns else ""
    out["venue"] = (df["venue"].fillna("") if "venue" in df.columns
                    else (df["booktitle"].fillna("") if "booktitle" in df.columns else ""))
    out["source"] = name
    out["uid"] = df["uid"].fillna("").astype(str) if "uid" in df.columns else ""
    out["pmid"] = df["pmid"].fillna("").astype(str) if "pmid" in df.columns else ""
    out["times_cited"] = (df["times_cited"].fillna(0).astype(int)
                          if "times_cited" in df.columns else 0)
    out["keywords"] = (df["keywords"].apply(lambda v: v if isinstance(v, str) else "")
                       if "keywords" in df.columns else "")
    out["mesh_terms"] = (df["mesh_terms"].apply(lambda v: v if isinstance(v, str) else "")
                         if "mesh_terms" in df.columns else "")
    # DBLP carries the type under `type`; WOS under `doc_type`. Unify them.
    out["doc_type"] = (df["doc_type"].fillna("").astype(str) if "doc_type" in df.columns
                       else (df["type"].fillna("").astype(str) if "type" in df.columns
                             else ""))
    return out


# ================================================================ stage 1
def stage_merge(args) -> int:
    print("=" * 72)
    print("STAGE 1 - merge raw source tables")
    print("=" * 72)

    missing = [n for n, p in SOURCES.items() if not p.exists()]
    if missing:
        print(f"[error] missing source files: {missing}")
        return 1

    frames = [load_normalized(n, p) for n, p in SOURCES.items()]
    raw_counts = {n: len(f) for n, f in zip(SOURCES, frames)}
    df = pd.concat(frames, ignore_index=True)
    total = len(df)
    print(f"  concatenated{total:>9,} rows")

    led = Ledger("Stage 1 - merge", total)
    removed_frames = []

    # ---- de-duplicate by DOI ----
    has_doi = df["doi"].ne("")
    no_doi = df[~has_doi].copy()
    dup_doi = df[has_doi & df["doi"].duplicated(keep="first")]
    merged_doi = df[has_doi].groupby("doi", sort=False).agg(
        title=("title", first_nonempty), authors=("authors", merge_unique),
        doi=("doi", first_nonempty), abstract=("abstract", first_nonempty),
        year=("year", first_nonempty), journal=("journal", first_nonempty),
        venue=("venue", first_nonempty), source=("source", merge_unique),
        uid=("uid", merge_unique), pmid=("pmid", merge_unique),
        times_cited=("times_cited", "max"), keywords=("keywords", merge_unique),
        mesh_terms=("mesh_terms", merge_unique),
        doc_type=("doc_type", merge_unique),
    ).reset_index(drop=True)
    removed_frames.append(dup_doi.assign(drop_reason="duplicate DOI"))
    led.step("De-duplicate by DOI", int(has_doi.sum()) - len(merged_doi),
             len(merged_doi) + len(no_doi),
             note=f"with DOI {int(has_doi.sum()):,} -> {len(merged_doi):,}")

    # ---- no-DOI records: normalized title + surname overlap ----
    no_doi["title_norm"] = no_doi["title"].apply(norm_title)
    no_doi["surnames"] = no_doi["authors"].apply(authors_surnames)
    keep, by_title, dup_idx = [], {}, []
    for idx, row in no_doi.iterrows():
        t = row["title_norm"]
        if not t:
            keep.append(idx)
            continue
        grp = by_title.setdefault(t, [])
        hit = None
        for g in grp:
            if not row["surnames"] or not g["surnames"] or (row["surnames"] & g["surnames"]):
                hit = g
                break
        if hit is None:
            grp.append({"idx": idx, "surnames": row["surnames"]})
            keep.append(idx)
        else:
            dup_idx.append(idx)
            rep = hit["idx"]
            for col in MERGE_COLS:
                cur, new = no_doi.at[rep, col], row[col]
                if col in UNIQUE_COLS or col == "doc_type":
                    no_doi.at[rep, col] = merge_unique(pd.Series([cur, new]))
                elif (not isinstance(cur, str) or not cur.strip()) and isinstance(new, str):
                    no_doi.at[rep, col] = new
            no_doi.at[rep, "times_cited"] = max(no_doi.at[rep, "times_cited"],
                                                row["times_cited"])
            if isinstance(row["surnames"], set):
                no_doi.at[rep, "surnames"] = hit["surnames"] | row["surnames"]

    if dup_idx:
        removed_frames.append(
            no_doi.loc[dup_idx].drop(columns=["title_norm", "surnames"], errors="ignore")
            .assign(drop_reason="duplicate title + author overlap"))
    no_doi_dedup = (no_doi.loc[keep].drop(columns=["title_norm", "surnames"])
                    .reset_index(drop=True))
    led.step("De-duplicate no-DOI records", len(no_doi) - len(no_doi_dedup),
             len(merged_doi) + len(no_doi_dedup),
             note="normalized title + surname overlap")

    # ---- collapse identical normalized titles among the no-DOI remainder ----
    no_doi_dedup["title_norm"] = no_doi_dedup["title"].apply(norm_title)
    dup_titles = no_doi_dedup["title_norm"][
        no_doi_dedup["title_norm"].ne("")
        & no_doi_dedup["title_norm"].duplicated(keep=False)].unique()

    if len(dup_titles):
        mask_dup = no_doi_dedup["title_norm"].isin(dup_titles)
        grp = no_doi_dedup[mask_dup].groupby("title_norm", sort=False)
        merged_dup = grp.agg(
            title=("title", first_nonempty), authors=("authors", merge_unique),
            abstract=("abstract", first_nonempty), year=("year", first_nonempty),
            journal=("journal", first_nonempty), venue=("venue", first_nonempty),
            source=("source", merge_unique), uid=("uid", merge_unique),
            pmid=("pmid", merge_unique), times_cited=("times_cited", "max"),
            keywords=("keywords", merge_unique), mesh_terms=("mesh_terms", merge_unique),
            doc_type=("doc_type", merge_unique),
        ).reset_index(drop=True)
        merged_dup["doi"] = ""
        nod_keep = no_doi_dedup[~mask_dup].drop(columns=["title_norm"])
        removed = len(no_doi_dedup) - len(nod_keep) - len(merged_dup)
        # audit: every member of a collapsed group except the representative
        ex = (no_doi_dedup[mask_dup].sort_values("title_norm")
              .groupby("title_norm", sort=False).apply(
                  lambda g: g.iloc[1:], include_groups=False).reset_index(drop=True))
        if len(ex):
            removed_frames.append(ex.assign(drop_reason="identical normalized title"))
        led.step("Collapse identical titles", max(removed, 0),
                 len(merged_doi) + len(nod_keep) + len(merged_dup),
                 note=f"{len(dup_titles)} title group(s)")
    else:
        merged_dup = pd.DataFrame(columns=merged_doi.columns)
        nod_keep = no_doi_dedup.drop(columns=["title_norm"])
        led.step("Collapse identical titles", 0, len(merged_doi) + len(nod_keep))

    final = pd.concat([merged_doi, nod_keep, merged_dup], ignore_index=True)
    for c in ["uid", "pmid", "keywords", "mesh_terms", "doc_type", "venue"]:
        if c not in final.columns:
            final[c] = ""
    final = final.sort_values(["source", "year"]).reset_index(drop=True)

    # ---- audit CSV for everything dropped in this stage ----
    if removed_frames:
        rem = pd.concat(removed_frames, ignore_index=True)
        for c in AUDIT_COLS:
            if c not in rem.columns:
                rem[c] = ""
        csv = dump_removed(rem, OUT_MERGE, AUDIT_COLS + ["drop_reason"])
    else:
        csv = None
    led.finish()
    led.write_summary()

    print(f"\n  source rows: " + ", ".join(f"{k}={v:,}" for k, v in raw_counts.items()))
    print(f"  with DOI: {int(final['doi'].ne('').sum()):,} "
          f"({final['doi'].ne('').mean()*100:.1f}%)")
    print(f"  with abstract: {int(final['abstract'].str.strip().ne('').sum()):,} "
          f"({final['abstract'].str.strip().ne('').mean()*100:.1f}%)")

    final.to_parquet(MERGED, index=False)
    print(f"\n  saved -> {rel(MERGED)}  ({len(final):,} rows)")
    append_report(led.to_markdown())
    return 0


# ================================================================ stage 2
def stage_enrich(args) -> int:
    src = Path(getattr(args, "file", None) or MERGED)
    print("=" * 72)
    print("STAGE 2 - enrich fields from OpenAlex")
    print("=" * 72)
    if not src.exists():
        print(f"[error] {src} not found - run the merge stage first")
        return 1

    df = pd.read_parquet(src)
    n0 = len(df)
    print(f"  rows: {n0:,}  file: {rel(src)}")

    df["_doi"] = df["doi"].apply(normalize_doi)
    has_doi = df["_doi"].ne("")
    print(f"  with DOI: {int(has_doi.sum()):,}")

    led = Ledger("Stage 2 - enrich", n0)

    # ---- A. look up by DOI ----
    ck = Ckpt(CHECKPOINTS["doi"], "doi")
    dois = df.loc[has_doi, "_doi"].unique().tolist()
    pending = [d for d in dois if d not in ck.store]
    if getattr(args, "limit", None):
        pending = pending[: args.limit]
    print(f"  pending unique DOIs: {len(pending):,} -> "
          f"~{max(1, -(-len(pending)//BATCH_DOI)):,} batch requests")

    client = make_client("doi", getattr(args, "api_key", "") or "")
    for i in range(0, len(pending), BATCH_DOI):
        batch = pending[i:i + BATCH_DOI]
        ck.store.update(lookup_dois_batch(client, batch))
        for d in batch:
            ck.store.setdefault(d, {})        # remember the miss, do not retry forever
        done = i + len(batch)
        if done % (BATCH_DOI * 20) == 0 or done >= len(pending):
            ck.save()
            print(f"    {done:,}/{len(pending):,} DOIs queried")
    ck.save()

    for col in ["abstract", "authors_std", "affiliations_std"]:
        if col not in df.columns:
            df[col] = ""

    filled = {"abstract": 0, "authors_std": 0, "affiliations_std": 0}
    for idx in df.index[has_doi]:
        rec = ck.store.get(df.at[idx, "_doi"])
        if not rec:
            continue
        if rec.get("abstract") and not df.at[idx, "abstract"]:
            df.at[idx, "abstract"] = rec["abstract"]
            filled["abstract"] += 1
        if rec.get("authors_std"):
            df.at[idx, "authors_std"] = rec["authors_std"]
            filled["authors_std"] += 1
        if rec.get("affiliations_std"):
            df.at[idx, "affiliations_std"] = rec["affiliations_std"]
            filled["affiliations_std"] += 1

    led.step("Fill fields by DOI", 0, n0,
             note=f"abstract +{filled['abstract']:,}, "
                  f"authors_std +{filled['authors_std']:,}, "
                  f"affiliations_std +{filled['affiliations_std']:,}")

    # ---- B. records still lacking author structure ----
    miss = df["authors_std"].astype(str).str.strip().eq("")
    n_missing = int(miss.sum())
    csv = dump_removed(df[miss], OUT_ENRICH_MISSING) if n_missing else None
    led.step("Still missing authors_std", n_missing, n0 - n_missing, csv,
             note="run `enrich.py title` to fill by title search")

    df = df.drop(columns=["_doi"])
    write_back(df, src)

    have_abs = df["abstract"].astype(str).str.strip().ne("")
    have_auth = df["authors_std"].astype(str).str.strip().ne("")
    print(f"\n  with abstract:     {int(have_abs.sum()):,} ({have_abs.mean()*100:.1f}%)")
    print(f"  with authors_std:  {int(have_auth.sum()):,} ({have_auth.mean()*100:.1f}%)")
    led.finish()
    led.write_summary()
    append_report(led.to_markdown())
    return 0


# ================================================================ stage 3
def is_non_english(title) -> bool:
    """True when a title is dominated by a non-Latin writing system."""
    if not isinstance(title, str) or not title.strip():
        return False
    nl = len(NON_LATIN_RE.findall(title))
    la = len(LATIN_RE.findall(title))
    if nl + la == 0:
        return False
    return nl / (nl + la) >= NON_EN_THRESHOLD


def type_to_drop(doc_type) -> bool:
    """True when any comma/semicolon-separated token is in DROP_TYPES."""
    if not isinstance(doc_type, str) or not doc_type.strip():
        return False
    parts = [p.strip().lower() for p in re.split(r"[;,]", doc_type)]
    return any(p in DROP_TYPES for p in parts)


def stage_clean(args) -> int:
    src = Path(getattr(args, "file", None) or MERGED)
    print("=" * 72)
    print("STAGE 3 - clean")
    print("=" * 72)
    if not src.exists():
        print(f"[error] {src} not found - run the merge / enrich stage first")
        return 1

    df = pd.read_parquet(src)
    n0 = len(df)
    wos = df["source"].str.contains("wos", na=False)
    print(f"  input: {n0:,} rows (WOS {int(wos.sum()):,} / "
          f"non-WOS {int((~wos).sum()):,})")

    wos_only = bool(getattr(args, "wos_only", False))
    if wos_only:
        print("  --wos-only: the three filters apply to WOS rows only")

    led = Ledger("Stage 3 - clean", n0)
    in_scope = wos if wos_only else pd.Series(True, index=df.index)

    # ---- 1. non-English titles ----
    m1 = in_scope & df["title"].apply(is_non_english)
    csv1 = dump_removed(df[m1], OUT_CLEAN_NON_EN) if m1.any() else None
    df = df[~m1].reset_index(drop=True)
    led.step("Non-English titles", int(m1.sum()), len(df), csv1,
             note=f"non-Latin character share >= {NON_EN_THRESHOLD:.0%}")

    # ---- 2. editorial / letter / review document types ----
    scope2 = (df["source"].str.contains("wos", na=False) if wos_only
              else pd.Series(True, index=df.index))
    m2 = (scope2 & df["doc_type"].apply(type_to_drop)
          if "doc_type" in df.columns else pd.Series(False, index=df.index))
    csv2 = dump_removed(df[m2], OUT_CLEAN_TYPE) if m2.any() else None
    df = df[~m2].reset_index(drop=True)
    led.step("Editorial / letter / review", int(m2.sum()), len(df), csv2,
             note=f"{len(DROP_TYPES)} tokens matched on doc_type")

    # ---- 3. records without an abstract (relaxed) ----
    #
    # The WoS Starter API returns NO abstract for any record - measured, 0 of
    # 143,433. Every abstract in the corpus comes from the OpenAlex enrichment
    # in stage 2, so this rule really drops "OpenAlex has no abstract for it".
    #
    # Dropping all of them costs the urban-modelling literature
    # disproportionately: of Batty's 53 WoS records matching the query, 15 never
    # get an OpenAlex abstract, and every one of them was being deleted here.
    #
    # So the rule is now: an abstractless record is dropped UNLESS its TITLE
    # carries a strong ABM anchor. On the last full run 14,045 of the 55,641
    # abstractless rows qualified. They are marked with `abstract_missing` so a
    # topic / LLM analysis can exclude them, while still counting for the
    # bibliometric and author-level work, which only needs title + authors.
    abs_missing = df["abstract"].astype(str).str.strip().eq("")
    title_anchor = df["title"].fillna("").astype(str).str.contains(
        STRONG_ANCHOR_RE, na=False)
    strict_noabs = bool(getattr(args, "strict_no_abstract", False))
    if strict_noabs:
        m3 = abs_missing
        kept_noabs = pd.Series(False, index=df.index)
        print("  --strict-no-abstract: every abstractless row is dropped")
    else:
        m3 = abs_missing & ~title_anchor
        kept_noabs = abs_missing & title_anchor
    csv3 = dump_removed(df[m3], OUT_CLEAN_NOABS) if m3.any() else None
    if kept_noabs.any():
        cols = [c for c in AUDIT_COLS if c in df.columns]
        df.loc[kept_noabs, cols].to_csv(OUT_CLEAN_KEPT_NOABS, index=False,
                                        encoding="utf-8-sig")
    keep_mask = ~m3
    n_kept_noabs = int(kept_noabs.sum())
    df = df[keep_mask].reset_index(drop=True)
    df["abstract_missing"] = kept_noabs[keep_mask].to_numpy()
    led.step("No abstract (kept if title has an ABM anchor)", int(m3.sum()), len(df), csv3,
             note=("strict: all abstractless dropped" if strict_noabs else
                   f"kept {n_kept_noabs:,} abstractless rows whose title carries "
                   f"an ABM anchor -> {OUT_CLEAN_KEPT_NOABS.name}"))

    # ---- optional: zero-cited ----
    if getattr(args, "drop_zero_cited", False):
        m4 = df["times_cited"].fillna(0).eq(0)
        csv4 = dump_removed(df[m4], OUT_CLEAN_ZERO) if m4.any() else None
        df = df[~m4].reset_index(drop=True)
        led.step("Zero-cited (optional)", int(m4.sum()), len(df), csv4,
                 note="--drop-zero-cited")

    led.finish()
    led.write_summary()

    print(f"\n  output: {len(df):,} rows")
    print("  by source: " + ", ".join(f"{k}={v:,}" for k, v in
                                      df["source"].value_counts().items()))
    print(f"  with DOI: {int(df['doi'].ne('').sum()):,} "
          f"({df['doi'].ne('').mean()*100:.1f}%)")

    df.to_parquet(FILTERED, index=False)
    print(f"\n  saved -> {rel(FILTERED)}")

    append_report(led.to_markdown())
    return 0


# ================================================================ CLI
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="pipeline.py",
        description="Literature pipeline: merge -> enrich -> clean, "
                    "with per-stage CSV audit trails.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--api-key", default="",
                    help="override the OpenAlex key (optional)")
    sub = ap.add_subparsers(dest="task")

    sub.add_parser("merge", help="merge the raw tables in database/ and de-duplicate")

    p = sub.add_parser("enrich", help="fill abstract / authors_std / affiliations_std")
    p.add_argument("--file", default=str(MERGED))
    p.add_argument("--limit", type=int, default=None, help="query only the first N DOIs")

    p = sub.add_parser("clean", help="drop non-English titles, editorial types, no-abstract rows")
    p.add_argument("--file", default=str(MERGED))
    p.add_argument("--wos-only", action="store_true",
                   help="apply the three filters to WOS rows only (legacy behaviour)")
    p.add_argument("--strict-no-abstract", action="store_true",
                   help="drop every abstractless row (old behaviour). By default "
                        "an abstractless row is kept when its title carries an ABM anchor")
    p.add_argument("--drop-zero-cited", action="store_true",
                   help="also drop zero-cited records (legacy behaviour; off by default)")

    p = sub.add_parser("run", help="run merge -> enrich -> clean in order")
    p.add_argument("--file", default=str(MERGED))
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--wos-only", action="store_true")
    p.add_argument("--strict-no-abstract", action="store_true")
    p.add_argument("--drop-zero-cited", action="store_true")
    return ap


def run_all(args) -> int:
    """Run the three stages in order and write the combined summary CSV.

    Each stage appends its own rows to process/stage_summary.csv as it finishes,
    so a failure midway still leaves the audit trail of the stages that ran.
    """
    for fn in (stage_merge, stage_enrich, stage_clean):
        rc = fn(args)
        if rc != 0:
            return rc
        print()
    return 0


def main() -> int:
    args = build_parser().parse_args()
    # Default to the full run when executed without arguments (PyCharm: just press Run).
    task = args.task or "run"

    if task == "merge":
        return stage_merge(args)
    if task == "enrich":
        return stage_enrich(args)
    if task == "clean":
        return stage_clean(args)
    if task == "run":
        return run_all(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
