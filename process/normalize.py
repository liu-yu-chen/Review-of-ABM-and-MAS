#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage 6: normalize the corpus into an analysis-ready table.

Standalone script. Runs directly in PyCharm (press Run) or from a terminal.

## What it fixes

The corpus produced by stages 1-5 is faithful to its sources, which means the
same concept appears in several spellings. Analysis code that groups by these
columns silently splits the groups it is meant to compare. The issues found by
profiling the 31,319-row corpus:

    doc_type      24 distinct spellings for ~6 real categories, including
                  multi-source concatenations such as "Article;article" and
                  "Meeting;inproceedings", plus 1,415 empty values
    journal       split across two columns; 2,261 rows carry only `venue`
    year          string, 3 unparseable, 1,081 rows dated 2026-2027
    authors_std   a JSON *string*, names in mixed case ("LIN" vs "Lin"),
                  Unicode variants (O\u2019Mahony, Clerin\u2010Debart)
    keywords      JSON strings, PubMed only
    times_cited   WOS only: 0 means "no data", not "never cited"

## Columns added

    venue_name        journal, falling back to venue
    doc_type_std      one of: article / review / proceedings / book /
                      thesis / editorial / retraction / other / unknown
    year_num          numeric year, NaN when unparseable
    authors_list      parsed authors_std, as a list of dicts
    n_authors         author count
    countries         distinct ISO country codes
    institutions      distinct institution names
    first_author      "First Last" of the first author
    corresponding     "First Last" of the corresponding author, if any
    has_orcid         whether any author carries an ORCID
    keywords_list     parsed keywords
    mesh_list         parsed MeSH terms
    name_key          normalized author string for de-duplication
                      (lowercase, no accents, hyphens/quotes unified)

## Outputs

    database/analysis_ready.parquet          the normalized table (new columns appended)
    process/stage6_normalize_report.csv      per-column before/after summary

## Usage

    python process/normalize.py
    python process/normalize.py --report-only
    python process/normalize.py --in X.parquet --out Y.parquet
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

import pandas as pd

PROCESS_DIR = Path(__file__).resolve().parent
ROOT = PROCESS_DIR.parent
DB = ROOT / "database"

SOURCE = DB / "analysis_literature.parquet"
OUTPUT = DB / "analysis_ready.parquet"
OUT_REPORT = PROCESS_DIR / "stage6_normalize_report.csv"

CURRENT_YEAR = 2025


# ============================================================ helpers
def rel(p: Path) -> str:
    try:
        return str(Path(p).relative_to(ROOT))
    except ValueError:
        return str(p)


def norm_name(s: str) -> str:
    """Normalize a person or institution name for matching.

    Applies NFKC (so full-width forms fold), strips combining accents, unifies
    the several dash and apostrophe codepoints that OpenAlex and WOS each emit,
    and collapses whitespace. Case is folded last.

    Example: 'O\\u2019Mahony' and "O'Mahony" both become "o'mahony";
    'Cl\\u00e9rin\\u2010Debart' becomes "clerin-debart".
    """
    s = unicodedata.normalize("NFKC", str(s or ""))
    # decompose accents, then drop the combining marks
    s = "".join(c for c in unicodedata.normalize("NFD", s)
                if unicodedata.category(c) != "Mn")
    s = re.sub(r"[\u2010-\u2015\u2212]", "-", s)      # dash variants -> hyphen
    s = re.sub(r"[\u2018\u2019\u02bc\u2032`]", "'", s)  # apostrophe variants
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def as_list(raw) -> list:
    """Parse a JSON-array string into a list; [] when empty or malformed."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    s = str(raw).strip()
    if not s or s == "[]":
        return []
    try:
        v = json.loads(s)
        return v if isinstance(v, list) else []
    except Exception:
        return []


def parse_people(raw) -> list[dict]:
    """Parse authors_std into a list of dicts, dropping malformed entries."""
    out = []
    for a in as_list(raw):
        if isinstance(a, dict) and (a.get("first_name") or a.get("last_name")):
            out.append(a)
    return out


def std_person(a: dict) -> str:
    """'First Last' with both parts whitespace-normalized."""
    f = re.sub(r"\s+", " ", str(a.get("first_name") or "")).strip()
    l = re.sub(r"\s+", " ", str(a.get("last_name") or "")).strip()
    return f"{f} {l}".strip()


# ============================================================ doc_type
# Rules are applied to each comma/semicolon-separated token, most specific
# first. The corpus has 24 raw spellings; these map onto 9 categories.
DOC_TYPE_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"retract", re.I), "retraction"),
    (re.compile(r"^(phdthesis|mastersthesis|thesis)$", re.I), "thesis"),
    # `incollection` is a book chapter in DBLP, so it must be matched before the
    # proceedings rule, which otherwise catches it via the `incollection` stem.
    (re.compile(r"^(book|booklet|incollection)$", re.I), "book"),
    (re.compile(r"^(inproceedings|proceedings|conference)", re.I), "proceedings"),
    (re.compile(r"^(editorial|letter|comment|note|erratum|correction)", re.I),
     "editorial"),
    (re.compile(r"^(review|survey)", re.I), "review"),
    (re.compile(r"^(article|journal article|data paper|early access|meeting)",
                re.I), "article"),
    (re.compile(r"^(news|biography|abstract)", re.I), "other"),
]


def std_doc_type(raw) -> str:
    """Collapse a raw doc_type into one of the canonical categories.

    Tokens are examined in order and the most specific rule wins, so
    "Article, Retracted Publication" becomes 'retraction', not 'article'.
    """
    s = str(raw or "").strip()
    if not s:
        return "unknown"
    tokens = [t.strip() for t in re.split(r"[;,]", s) if t.strip()]
    if not tokens:
        return "unknown"
    for pat, label in DOC_TYPE_RULES:
        for t in tokens:
            if pat.match(t):
                return label
    return "other"


# ============================================================ main
def main() -> int:
    ap = argparse.ArgumentParser(
        prog="normalize.py",
        description="Normalize the corpus into an analysis-ready table.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--in", dest="inp", default=str(SOURCE))
    ap.add_argument("--out", default=str(OUTPUT))
    ap.add_argument("--report-only", action="store_true",
                    help="print what would change, write nothing")
    args = ap.parse_args()

    src = Path(args.inp)
    if not src.exists():
        print(f"[error] {src} not found")
        return 1

    df = pd.read_parquet(src)
    n = len(df)
    print("=" * 72)
    print("STAGE 6 - normalize to analysis-ready table")
    print("=" * 72)
    print(f"  input: {n:,} rows x {len(df.columns)} cols")

    report: list[dict] = []

    # ---- 1. venue_name: journal, falling back to venue ----
    j = df["journal"].fillna("").astype(str).str.strip()
    v = df["venue"].fillna("").astype(str).str.strip()
    df["venue_name"] = j.where(j.ne(""), v)
    print(f"\n  venue_name: filled {int(df['venue_name'].ne('').sum()):,}/{n:,} "
          f"({df['venue_name'].ne('').mean()*100:.1f}%) "
          f"(journal {int(j.ne('').sum()):,} + venue-only {int((j.eq('') & v.ne('')).sum()):,})")
    report.append({"column": "venue_name", "issue": "split across journal/venue",
                   "before": f"journal {j.ne('').sum():,}; venue {v.ne('').sum():,}",
                   "after": f"{df['venue_name'].ne('').sum():,} filled"})

    # ---- 2. doc_type_std ----
    raw_types = df["doc_type"].astype(str).str.strip()
    df["doc_type_std"] = raw_types.map(std_doc_type)
    print(f"\n  doc_type_std: {raw_types.nunique()} raw spellings -> "
          f"{df['doc_type_std'].nunique()} categories")
    for k, c in df["doc_type_std"].value_counts().items():
        print(f"    {k:<12}{c:>7,}")
    report.append({"column": "doc_type_std",
                   "issue": f"{raw_types.nunique()} spellings, multi-source concatenations",
                   "before": f"{raw_types.nunique()} distinct",
                   "after": f"{df['doc_type_std'].nunique()} categories"})

    # ---- 3. year_num ----
    df["year_num"] = pd.to_numeric(df["year"], errors="coerce")
    n_bad = int(df["year_num"].isna().sum())
    n_future = int((df["year_num"] > CURRENT_YEAR).sum())
    print(f"\n  year_num: {n - n_bad:,} parsed, {n_bad} unparseable, "
          f"{n_future:,} dated after {CURRENT_YEAR} (kept, flagged by range)")
    report.append({"column": "year_num", "issue": "string year, some unparseable",
                   "before": f"{n_bad} unparseable",
                   "after": f"{n - n_bad:,} numeric; {n_future:,} in the future"})

    # ---- 4. authors_std -> structured columns ----
    people = df["authors_std"].map(parse_people)
    df["authors_list"] = people
    df["n_authors"] = people.map(len)
    df["first_author"] = people.map(
        lambda L: std_person(L[0]) if L else "")
    df["corresponding"] = people.map(
        lambda L: next((std_person(a) for a in L if a.get("is_corresponding")), ""))
    df["has_orcid"] = people.map(
        lambda L: any(str(a.get("orcid") or "").strip() for a in L))
    df["name_key"] = people.map(
        lambda L: ";".join(sorted({norm_name(std_person(a)) for a in L if std_person(a)})))

    n_people = int(df["n_authors"].gt(0).sum())
    print(f"\n  authors_list: parsed for {n_people:,}/{n:,} rows "
          f"({n_people/n*100:.1f}%)")
    print(f"    mean authors per paper: {df.loc[df['n_authors'] > 0, 'n_authors'].mean():.2f}")
    print(f"    single-author papers:   {int(df['n_authors'].eq(1).sum()):,}")
    print(f"    with a corresponding author: {int(df['corresponding'].ne('').sum()):,}")
    print(f"    with any ORCID:         {int(df['has_orcid'].sum()):,}")
    report.append({"column": "authors_list",
                   "issue": "authors_std was a JSON string; names in mixed case and "
                            "Unicode variants",
                   "before": f"{int(df['authors_std'].astype(str).str.strip().ne('').sum()):,} raw strings",
                   "after": f"{n_people:,} parsed; name_key normalized"})

    # ---- 5. countries / institutions ----
    def collect(raw, key: str) -> list[str]:
        out = []
        for inst in as_list(raw):
            if isinstance(inst, dict):
                val = str(inst.get(key) or "").strip()
                if val and val not in out:
                    out.append(val)
        return out

    aff = df["affiliations_std"]
    df["countries"] = aff.map(lambda r: collect(r, "country"))
    df["institutions"] = aff.map(lambda r: collect(r, "institution"))
    n_ctry = int(df["countries"].map(len).gt(0).sum())
    print(f"\n  countries: {n_ctry:,}/{n:,} rows ({n_ctry/n*100:.1f}%)")
    print(f"  institutions: {int(df['institutions'].map(len).gt(0).sum()):,} rows")
    ctr = Counter(c for L in df["countries"] for c in L)
    print("    top countries: " + ", ".join(f"{k}={v:,}" for k, v in ctr.most_common(6)))
    report.append({"column": "countries / institutions",
                   "issue": "nested JSON inside affiliations_std",
                   "before": "nested JSON string",
                   "after": f"{n_ctry:,} rows with country; {len(ctr)} distinct codes"})

    # ---- 6. keywords / mesh ----
    df["keywords_list"] = df["keywords"].map(as_list)
    df["mesh_list"] = df["mesh_terms"].map(as_list)
    n_kw = int(df["keywords_list"].map(len).gt(0).sum())
    n_mesh = int(df["mesh_list"].map(len).gt(0).sum())
    print(f"\n  keywords_list: {n_kw:,} rows   mesh_list: {n_mesh:,} rows "
          f"(PubMed-sourced only)")
    report.append({"column": "keywords_list / mesh_list",
                   "issue": "JSON strings, PubMed only",
                   "before": "JSON string",
                   "after": f"{n_kw:,} / {n_mesh:,} rows parsed"})

    # ---- 7. citation caveat ----
    tc = pd.to_numeric(df["times_cited"], errors="coerce").fillna(0)
    n_zero = int(tc.eq(0).sum())
    src_has_cit = df["source"].astype(str).str.contains("wos", na=False)
    print(f"\n  times_cited: {n_zero:,} zeros, of which "
          f"{int((tc.eq(0) & ~src_has_cit).sum()):,} come from sources that "
          f"never report citations (WOS-only field)")
    print("    -> 0 means 'no data', not 'never cited'; enrich from OpenAlex for real counts")
    report.append({"column": "times_cited",
                   "issue": "WOS-only; 0 conflates 'no data' with 'never cited'",
                   "before": f"{n_zero:,} zeros",
                   "after": f"{int((tc.eq(0) & src_has_cit).sum()):,} true zeros within WOS"})

    # ---- write ----
    if args.report_only:
        print("\n  [report-only] nothing written.")
        return 0

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_path, index=False)
    pd.DataFrame(report).to_csv(OUT_REPORT, index=False, encoding="utf-8-sig")

    print(f"\n  columns: {len(df.columns)} "
          f"(+{len(df.columns) - len(pd.read_parquet(src).columns)} new)")
    print(f"  saved -> {rel(out_path)}  ({n:,} rows)")
    print(f"  report -> {OUT_REPORT.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
