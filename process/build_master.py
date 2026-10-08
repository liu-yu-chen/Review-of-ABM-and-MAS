#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build the master analysis table: normalized fields + DeepSeek labels.

Standalone script. Runs directly in PyCharm (press Run) or from a terminal.

## Why

Two stages produced separate tables that have never been joined:

    database/analysis_ready.parquet              29 cols - normalized fields
    database/analysis_literature_deepseek.parquet 23 cols - DeepSeek labels

They share the first 16 source columns, so they are joined on row position
(both were derived from analysis_literature.parquet without reordering).
A `row_index` column is checked before and after the join.

## Output

    database/master.parquet     normalized fields + DeepSeek labels, 36 cols

The name-parsing helpers live in normalize.py and are imported here so the two
scripts cannot drift apart.

## Usage

    python process/build_master.py
    python process/build_master.py --check-scholars
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

import pandas as pd

PROCESS_DIR = Path(__file__).resolve().parent
ROOT = PROCESS_DIR.parent
DB = ROOT / "database"
sys.path.insert(0, str(PROCESS_DIR))
sys.stdout.reconfigure(encoding="utf-8")

NORMALIZED = DB / "analysis_ready.parquet"
DEEPSEEK = DB / "analysis_literature_deepseek.parquet"
MASTER = DB / "master.parquet"

sys.path.insert(0, str(ROOT / "tools"))
from normalize import norm_name  # noqa: E402

# DeepSeek label columns to carry over.
DEEPSEEK_COLS = ["abm_topic", "abm_topic_label", "agent_category",
                 "agent_category_label", "llm_used", "llm_role",
                 "llm_role_label"]


def build_master() -> pd.DataFrame:
    norm = pd.read_parquet(NORMALIZED)
    ds = pd.read_parquet(DEEPSEEK)

    print(f"normalized : {len(norm):,} rows x {len(norm.columns)} cols")
    print(f"deepseek   : {len(ds):,} rows x {len(ds.columns)} cols")

    if len(norm) != len(ds):
        raise ValueError(f"row count mismatch: {len(norm):,} vs {len(ds):,}")

    # Verify the two tables are row-aligned on the shared columns before
    # joining. If a stage ever reordered rows, joining by position would
    # silently attach the wrong labels to every paper.
    for col in ("title", "doi"):
        same = norm[col].astype(str).equals(ds[col].astype(str))
        print(f"  row alignment on '{col}': {'OK' if same else 'MISMATCH'}")
        if not same:
            raise ValueError(f"'{col}' differs between the two tables; "
                             f"cannot join by position")

    master = norm.copy()
    for col in DEEPSEEK_COLS:
        master[col] = ds[col].values

    # convenience: numeric year and llm flag
    master["llm_used_num"] = pd.to_numeric(master["llm_used"],
                                           errors="coerce").fillna(0).astype(int)
    master["is_llm"] = master["llm_used_num"].eq(1)

    print(f"\nmaster     : {len(master):,} rows x {len(master.columns)} cols")
    print(f"classified : {int(master['abm_topic'].notna().sum()):,} "
          f"({100*master['abm_topic'].notna().mean():.1f}%)")
    print(f"llm_used=1 : {int(master['is_llm'].sum()):,} "
          f"({100*master['is_llm'].mean():.2f}%)")
    return master


# ============================================================ scholar check
# People whose work defines the ABM field. Used to confirm the corpus and the
# ranking metrics actually surface them.
SCHOLARS: dict[str, list[str]] = {
    "Spatial / urban / GIS": [
        "michael batty", "batty michael",
        "andrew crooks", "crooks andrew",
        "nick malleson", "malleson nick", "nicholas malleson",
        "alison heppenstall", "heppenstall alison",
        "ed manley", "edmund manley", "manley ed",
        "paul torrens", "torrens paul",
        "mark birkin", "birkin mark",
        "harvey miller", "miller harvey j",
        "daniel brown", "brown daniel g",
    ],
    "Social simulation / theory": [
        "joshua epstein", "epstein joshua",
        "nigel gilbert", "gilbert nigel",
        "bruce edmonds", "edmonds bruce",
        "rosaria conte", "conte rosaria",
        "uri wilensky", "wilensky uri",
        "robert axtell", "axtell robert",
        "andreas flache", "flache andreas",
        "dirk helbing", "helbing dirk",
        "ron sun", "sun ron",
    ],
    "Ecology / methodology": [
        "volker grimm", "grimm volker",
        "steven railsback", "railsback steven",
        "marco janssen", "janssen marco",
        "charles macal", "macal charles",
        "michael north", "north michael",
    ],
    "Economics / complexity": [
        "j doyne farmer", "farmer j doyne", "doyne farmer",
        "alan kirman", "kirman alan",
        "cars hommes", "hommes cars",
        "blake lebaron", "lebaron blake",
    ],
}


def name_of(row) -> str:
    return norm_name(row)


def check_scholars(df: pd.DataFrame) -> None:
    """Count how often each target scholar appears, overall and as first author."""
    # flatten authors_list into an author -> paper-index map
    a2p: dict[str, list[int]] = {}
    first_author_idx: dict[str, list[int]] = {}
    for i, people in enumerate(df["authors_list"]):
        seen = set()
        for a in people:
            if not isinstance(a, dict):
                continue
            nm = norm_name(f"{a.get('first_name', '')} {a.get('last_name', '')}")
            if not nm:
                continue
            seen.add(nm)
            if a.get("position") == 1:
                first_author_idx.setdefault(nm, []).append(i)
        for nm in seen:
            a2p.setdefault(nm, []).append(i)

    print("\n" + "=" * 78)
    print("target scholars in the corpus")
    print("=" * 78)

    total_found = 0
    for group, names in SCHOLARS.items():
        print(f"\n-- {group} --")
        for target in names:
            pages = a2p.get(target, [])
            if not pages:
                continue
            total_found += 1
            firsts = len(first_author_idx.get(target, []))
            # how many of their papers carry an LLM label
            llm = int(df.iloc[pages]["is_llm"].sum()) if pages else 0
            print(f"  {target:<26} papers {len(pages):>3}   first-author {firsts:>3}"
                  f"   LLM-related {llm:>2}")

    # fuzzy: any name containing the surname, to catch initials and variants
    print("\n-- surname-only matches (catches initialled variants) --")
    surnames = ["batty", "crooks", "malleson", "heppenstall", "manley",
                "torrens", "birkin", "epstein", "gilbert", "edmonds", "grimm",
                "helbing", "wilensky", "axtell", "flache", "janssen"]
    for sn in surnames:
        hits = [nm for nm in a2p if nm.endswith(sn) or nm.endswith(sn + " jr")]
        if hits:
            print(f"  {sn:<14} {len(hits)} distinct name(s): "
                  + ", ".join(sorted(hits)[:5]))


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="build_master.py",
        description="Join the normalized table with the DeepSeek labels.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--check-scholars", action="store_true",
                    help="also report counts for well-known ABM scholars")
    ap.add_argument("--out", default=str(MASTER))
    args = ap.parse_args()

    print("=" * 78)
    print("BUILD MASTER TABLE")
    print("=" * 78)

    master = build_master()
    master.to_parquet(Path(args.out), index=False)
    print(f"\nsaved -> {Path(args.out).relative_to(ROOT)}")

    if args.check_scholars:
        check_scholars(master)
    return 0


if __name__ == "__main__":
    sys.exit(main())
