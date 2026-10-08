#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OpenAlex field enrichment - one script covering all four stages.

Standalone script. Runs directly in PyCharm (no arguments needed runs `doi`)
or from a terminal. The four stages were previously four separate scripts and
have been merged here with identical behaviour.

## Stages

    doi         Batch-look-up by DOI (100 per request) and fill
                abstract / authors_std / affiliations_std.  Writes back in place.
    title       For rows still missing authors_std, search by title and fill the
                same three columns. 8 concurrent workers. Writes back in place.
    citations   Fetch cited_by_count (optionally referenced_works). Does NOT
                touch the input file; emits data_cache/openalex_citations.parquet
    zc          Same author-field enrichment for the zero-cited corpus (jsonl).

    all         doi -> title -> citations.  (zc reads jsonl, so run it separately.)

## Fields written back

    abstract          full abstract text rebuilt from abstract_inverted_index
    authors_std       JSON list in author order; 7 keys per entry:
                      first_name / last_name / position / role /
                      is_corresponding / orcid / institutions[]
                      institutions entries carry 4 keys:
                      institution / country / type / ror
    affiliations_std  de-duplicated union of all author institutions

The citation cache carries four columns:
    doc_id / oa_id / oa_cited_by_count / oa_referenced_works

## Checkpoints

Each stage keeps its own checkpoint. Filenames match the original scripts, so
existing caches remain usable:

    database/enrich_checkpoint.json                (doi)
    database/search_checkpoint.json                (title)
    data_cache/openalex_citations_checkpoint.json  (citations)
    data_cache/zc_enrich_checkpoint.json           (zc)

Keyed by DOI / normalized title; safe to interrupt and resume. If the data is
re-collected and the old values should be discarded, delete the relevant
checkpoint first - citation counts grow over time, so the `citations` one is
the most important to refresh (or pass --reset).

## API key

The original scripts used two different keys (separate quotas). To keep
behaviour unchanged, each stage still uses its own default. Priority:
--api-key > OPENALEX_API_KEY environment variable > stage default.

## Usage

    python process/enrich.py doi --file database/merged_literature.parquet
    python process/enrich.py title --file database/merged_literature.parquet
    python process/enrich.py citations --scope all --references --reset
    python process/enrich.py zc
    python process/enrich.py all
    python process/enrich.py doi --limit 100          # trial run
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from pathlib import Path

import pandas as pd
import requests
from tqdm import tqdm

# Resolved from this file's location, so the script works when run from PyCharm
# with any working directory.
ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "database"
CACHE = ROOT / "data_cache"
CACHE.mkdir(exist_ok=True)
sys.stdout.reconfigure(encoding="utf-8")

API = "https://api.openalex.org/works"
MAILTO = "liuyc091502@outlook.com"

DEFAULT_KEYS = {
    "doi": "6n8PKhioAwAdLT97xMxb4b",
    "title": "GkuTRhS5t7kxuk9YFJDzi1",
    "citations": "6n8PKhioAwAdLT97xMxb4b",
    "zc": "6n8PKhioAwAdLT97xMxb4b",
}
CHECKPOINTS = {
    "doi": DB / "enrich_checkpoint.json",
    "title": DB / "search_checkpoint.json",
    "citations": CACHE / "openalex_citations_checkpoint.json",
    "zc": CACHE / "zc_enrich_checkpoint.json",
}

BATCH_DOI = 100          # OpenAlex filter OR-value limit (verified)
BATCH_TITLE = 30         # titles per OR request, kept URL-length safe
PAGE_SIZE = 100          # per-page cap
TITLE_SIM_THRESHOLD = 0.75
CONCURRENCY = 8
MAX_RETRIES = 5
TIMEOUT = 60
SAVE_EVERY_BATCHES = 20

SELECT = "id,doi,title,abstract_inverted_index,authorships"


# ====================================================================== client
class Client:
    """OpenAlex client with auth, rate limiting and back-off retry.

    Rate: ~100 req/s with a key, 10 req/s without.
    429 backs off exponentially, 5xx linearly, 401/403 reports and returns.
    """

    def __init__(self, api_key: str = ""):
        self.api_key = (api_key or "").strip()
        self.interval = 0.012 if self.api_key else 0.11
        self.session = requests.Session()
        headers = {"User-Agent": f"mailto:{MAILTO}"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        self.session.headers.update(headers)
        self._last = 0.0
        self._lock = threading.Lock()   # the title stage is multi-threaded

    def _rate_limit(self) -> None:
        with self._lock:
            now = time.time()
            elapsed = now - self._last
            if self._last > 0 and elapsed < self.interval:
                time.sleep(self.interval - elapsed)
            self._last = time.time()

    def get(self, params: dict):
        """Single GET. Returns parsed JSON, or None on miss/failure."""
        for attempt in range(1, MAX_RETRIES + 1):
            self._rate_limit()
            try:
                r = self.session.get(API, params=params, timeout=TIMEOUT)
                if r.status_code == 200:
                    return r.json()
                if r.status_code == 404:
                    return None
                if r.status_code in (401, 403):
                    print(f"\n  [auth] HTTP {r.status_code} - API key invalid or "
                          f"insufficient")
                    return None
                if r.status_code == 429:
                    wait = 2 ** attempt * 2
                    print(f"\n  [429] rate limited, waiting {wait}s ...")
                    time.sleep(wait)
                    continue
                if r.status_code >= 500:
                    time.sleep(2 ** attempt)
                    continue
                return None
            except requests.RequestException as e:
                print(f"\n  [net] {type(e).__name__}: {e} (attempt {attempt})")
                time.sleep(2 ** attempt)
        return None


# ====================================================================== normalize
def normalize_doi(raw) -> str:
    """Extract the `10.xxxx/...` part of any DOI string; lowercase, strip trailing punctuation."""
    if not isinstance(raw, str):
        return ""
    m = re.search(r"10\.\S+", raw.strip().lower())
    return m.group(0).rstrip(".)") if m else ""


def norm_title(raw) -> str:
    """Normalize a title for checkpoint keys and similarity comparison."""
    if not isinstance(raw, str):
        return ""
    s = re.sub(r"[^\w\s]", "", raw.lower())
    return re.sub(r"\s+", " ", s).strip()


def rebuild_abstract(inverted_index) -> str:
    """Rebuild readable abstract text from an OpenAlex abstract_inverted_index."""
    if not inverted_index:
        return ""
    pos = {}
    for word, idxs in inverted_index.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[i] for i in sorted(pos))


def split_name(display_name, raw_name):
    """Split an author name into (first_name, last_name).

    Prefers the 'Last, First' form of raw_author_name (WOS style), otherwise
    takes the last whitespace-separated token of display_name as the surname.
    """
    raw = (raw_name or "").strip()
    if "," in raw:
        parts = [p.strip() for p in raw.split(",", 1)]
        if len(parts) == 2 and parts[0] and parts[1]:
            return parts[1], parts[0]
    name = (display_name or "").strip()
    if not name:
        return "", ""
    tokens = name.split()
    if len(tokens) == 1:
        return "", tokens[0]
    return " ".join(tokens[:-1]), tokens[-1]


def parse_authorships(authorships):
    """Return (authors_std, affiliations_std) as two JSON strings.

    authors_std follows author order and each entry carries that author's own
    institutions; affiliations_std is the de-duplicated union across all
    authors, in first-appearance order.
    """
    authors, affils, seen = [], [], set()
    for pos, a in enumerate(authorships or [], start=1):
        author = a.get("author") or {}
        display_name = (author.get("display_name") or "").strip()
        if not display_name:
            continue
        first_name, last_name = split_name(display_name, a.get("raw_author_name"))
        inst_list = []
        for inst in a.get("institutions") or []:
            iname = (inst.get("display_name") or "").strip()
            if not iname:
                continue
            inst_list.append({
                "institution": iname,
                "country": (inst.get("country_code") or "").strip(),
                "type": (inst.get("type") or "").strip(),
                "ror": (inst.get("ror") or "").strip(),
            })
            key = iname.lower()
            if key not in seen:
                seen.add(key)
                affils.append(inst_list[-1])
        authors.append({
            "first_name": first_name,
            "last_name": last_name,
            "position": pos,
            "role": (a.get("author_position") or "").strip(),
            "is_corresponding": bool(a.get("is_corresponding")),
            "orcid": (author.get("orcid") or "").strip(),
            "institutions": inst_list,
        })
    return (json.dumps(authors, ensure_ascii=False),
            json.dumps(affils, ensure_ascii=False))


def source_surnames(authors_field) -> set:
    """Surnames from a 'A; B; C' author string, for same-title disambiguation."""
    out = set()
    for name in str(authors_field).split(";"):
        name = name.strip()
        if not name:
            continue
        if "," in name:
            out.add(name.split(",")[0].strip().lower())
        else:
            toks = name.split()
            if toks:
                out.add(toks[-1].lower())
    return out


# ====================================================================== checkpoint
class Ckpt:
    """JSON checkpoint with atomic writes (.tmp then os.replace)."""

    def __init__(self, path: Path, key: str):
        self.path = path
        self.key = key
        self.data = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                self.data = {}
        self.data.setdefault(key, {})

    @property
    def store(self) -> dict:
        return self.data[self.key]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def backup_parquet(self, target: Path) -> None:
        """Keep one .bak before writing back; never overwrite an existing .bak."""
        bak = Path(str(target) + ".bak")
        if not bak.exists():
            shutil.copy2(target, bak)
            print(f"backup created: {bak}")


def write_back(df: pd.DataFrame, src: Path) -> None:
    tmp = Path(str(src) + ".tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, src)
    print(f"written {src}")


def fill_report(df: pd.DataFrame, tag: str, filled: dict) -> None:
    n = len(df)
    print(f"\n--- after {tag} ---")
    for col, cnt in filled.items():
        have = int(df[col].astype(str).str.strip().ne("").sum())
        print(f"{col:<18}{have:>10,}/{n:,} ({have/n*100:6.2f}%)  filled {cnt:,}")


def make_client(stage: str, explicit: str = "") -> Client:
    """Key priority: --api-key > OPENALEX_API_KEY > stage default."""
    return Client(explicit or os.environ.get("OPENALEX_API_KEY")
                  or DEFAULT_KEYS[stage])


# ====================================================================== stage: doi
def lookup_dois_batch(client: Client, dois: list) -> dict:
    """One request for up to BATCH_DOI DOIs -> {doi: {abstract, authors_std, affiliations_std}}"""
    if not dois:
        return {}
    data = client.get({"filter": "doi:" + "|".join(dois),
                       "per-page": BATCH_DOI, "select": SELECT})
    out = {}
    if not data:
        return out
    for w in data.get("results", []):
        doi = normalize_doi(w.get("doi"))
        if not doi:
            continue
        a_std, aff_std = parse_authorships(w.get("authorships"))
        out[doi] = {
            "abstract": rebuild_abstract(w.get("abstract_inverted_index")),
            "authors_std": a_std,
            "affiliations_std": aff_std,
        }
    return out


def stage_doi(args) -> int:
    src = Path(args.file)
    if not src.exists():
        print(f"[error] {src} not found")
        return 1
    df = pd.read_parquet(src)
    print(f"rows: {len(df):,}  file: {src}")

    df["_doi"] = df["doi"].apply(normalize_doi)
    has_doi = df["_doi"].ne("")
    print(f"with DOI: {int(has_doi.sum()):,}")

    ck = Ckpt(CHECKPOINTS["doi"], "doi")
    dois = df.loc[has_doi, "_doi"].unique().tolist()
    pending = [d for d in dois if d not in ck.store]
    if args.limit:
        pending = pending[:args.limit]
    print(f"pending unique DOIs: {len(pending):,} -> "
          f"~{max(1, -(-len(pending)//BATCH_DOI)):,} batch requests")

    client = make_client("doi", args.api_key)
    for i in tqdm(range(0, len(pending), BATCH_DOI),
                  desc="OpenAlex DOI batch", unit="batch", ncols=100):
        batch = pending[i:i + BATCH_DOI]
        ck.store.update(lookup_dois_batch(client, batch))
        for d in batch:
            ck.store.setdefault(d, {})       # remember misses, do not retry forever
        if (i // BATCH_DOI + 1) % SAVE_EVERY_BATCHES == 0:
            ck.save()
    ck.save()
    print("lookups done")

    ck.backup_parquet(src)
    for col in ["abstract", "authors_std", "affiliations_std"]:
        if col not in df.columns:
            df[col] = ""

    filled = {"abstract": 0, "authors_std": 0, "affiliations_std": 0}
    for idx in tqdm(df.index[has_doi], desc="Write back", unit="row", ncols=100):
        rec = ck.store.get(df.at[idx, "_doi"])
        if not rec:
            continue
        # abstract is only filled when empty; author and affiliation fields
        # always overwrite (matching the original script).
        if rec.get("abstract") and not df.at[idx, "abstract"]:
            df.at[idx, "abstract"] = rec["abstract"]
            filled["abstract"] += 1
        if rec.get("authors_std"):
            df.at[idx, "authors_std"] = rec["authors_std"]
            filled["authors_std"] += 1
        if rec.get("affiliations_std"):
            df.at[idx, "affiliations_std"] = rec["affiliations_std"]
            filled["affiliations_std"] += 1

    df = df.drop(columns=["_doi"])
    write_back(df, src)
    fill_report(df, "DOI enrichment", filled)
    return 0


# ====================================================================== stage: title
def _best_hit(raw_title: str, works: list):
    """Pick the highest-similarity candidate title; prefer one with an abstract on ties."""
    tn = norm_title(raw_title)
    best = None
    for w in works:
        sim = SequenceMatcher(None, tn, norm_title(w.get("title") or "")).ratio()
        if sim < TITLE_SIM_THRESHOLD:
            continue
        has_abs = bool(w.get("abstract_inverted_index"))
        if best is None or sim > best[0] or (sim == best[0] and has_abs and not best[4]):
            a_std, aff_std = parse_authorships(w.get("authorships"))
            best = (sim, rebuild_abstract(w.get("abstract_inverted_index")),
                    a_std, aff_std, has_abs)
    return best


def search_titles_or(client: Client, raw_titles: list):
    """One request for up to BATCH_TITLE titles -> ({norm_title: (abs, a, aff)}, truncated).

    `truncated` is True when the API returned fewer works than matched, meaning
    the unmatched titles need a dedicated per-title retry.
    """
    out = {}
    flt = "title.search:" + "|".join(f'"{t}"' for t in raw_titles)
    data = client.get({"filter": flt, "per-page": PAGE_SIZE, "select": SELECT})
    if not data:
        return out, True
    works = data.get("results", [])
    truncated = data.get("meta", {}).get("count", 0) > len(works)
    for raw in raw_titles:
        best = _best_hit(raw, works)
        if best is not None:
            out[norm_title(raw)] = (best[1], best[2], best[3])
    return out, truncated


def search_title_single(client: Client, raw_title: str):
    data = client.get({"filter": f'title.search:"{raw_title}"',
                       "per-page": PAGE_SIZE, "select": SELECT})
    if not data:
        return None, None, None
    best = _best_hit(raw_title, data.get("results", []))
    return (None, None, None) if best is None else (best[1], best[2], best[3])


def stage_title(args) -> int:
    src = Path(args.file)
    if not src.exists():
        print(f"[error] {src} not found")
        return 1
    df = pd.read_parquet(src)
    print(f"rows: {len(df):,}  file: {src}")

    missing = df["authors_std"].astype(str).str.strip().eq("")
    print(f"rows missing authors_std: {int(missing.sum()):,}")

    sub = df[missing].copy()
    sub["_tn"] = sub["title"].apply(norm_title)
    sub["_has_title"] = sub["_tn"].ne("")
    titles = sub.loc[sub["_has_title"], "_tn"].unique().tolist()
    if args.limit:
        titles = titles[:args.limit]
    print(f"unique titles: {len(titles):,} -> "
          f"~{max(1, -(-len(titles)//BATCH_TITLE)):,} batch requests")

    ck = Ckpt(CHECKPOINTS["title"], "title")
    pending = [t for t in titles if t not in ck.store]
    print(f"pending: {len(pending):,}")

    # one representative raw title and author string per normalized key
    rep_raw, rep_authors = {}, {}
    for idx in sub.index[sub["_has_title"]]:
        tn = sub.at[idx, "_tn"]
        if tn not in rep_raw and sub.at[idx, "title"]:
            rep_raw[tn] = sub.at[idx, "title"]
            rep_authors[tn] = sub.at[idx, "authors"]

    client = make_client("title", args.api_key)
    n_req = 0
    retry_queue = []
    batches = [pending[i:i + BATCH_TITLE] for i in range(0, len(pending), BATCH_TITLE)]
    batch_iter = tqdm(batches, desc="OR title search", unit="batch", ncols=100)

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        futures = {ex.submit(search_titles_or, client,
                             [rep_raw[t] for t in b if t in rep_raw]): b
                   for b in batches}
        for fut in as_completed(futures):
            batch = futures[fut]
            try:
                results, truncated = fut.result()
            except Exception as e:
                print(f"[err] batch failed: {e}")
                results, truncated = {}, True
            n_req += 1

            matched = set()
            for t in batch:
                if t not in results:
                    ck.store[t] = {"abstract": "", "authors_std": "",
                                   "affiliations_std": ""}
                    continue
                abstract, a_std, aff_std = results[t]
                # surname spot-check: when the source record has surnames, require
                # at least one to appear among the candidate's authors, so that
                # same-title-different-paper matches are rejected.
                src_sur = source_surnames(rep_authors.get(t, ""))
                if src_sur and a_std:
                    hit_names = {a.get("last_name", "").lower()
                                 for a in json.loads(a_std)}
                    if not (src_sur & {n for n in hit_names if n}):
                        abstract, a_std, aff_std = "", "", ""
                if a_std:
                    matched.add(t)
                ck.store[t] = {"abstract": abstract, "authors_std": a_std,
                               "affiliations_std": aff_std}

            # defer per-title retries to after all batches, so truncation never
            # stalls the batch pipeline
            if truncated:
                for t in batch:
                    if t not in matched:
                        retry_queue.append((t, rep_raw.get(t, "")))

            batch_iter.update(1)
            if n_req % SAVE_EVERY_BATCHES == 0:
                ck.save()

    ck.save()
    print(f"batch lookups done ({n_req:,} requests); "
          f"{len(retry_queue):,} titles deferred for per-title retry")

    if retry_queue:
        it = tqdm(retry_queue, desc="Per-title retry", unit="title", ncols=100)
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
            futures = {ex.submit(search_title_single, client, raw): t
                       for t, raw in retry_queue}
            for fut in as_completed(futures):
                t = futures[fut]
                try:
                    abstract, a_std, aff_std = fut.result()
                except Exception as e:
                    print(f"[err] per-title failed: {e}")
                    abstract, a_std, aff_std = None, None, None
                n_req += 1
                if a_std:
                    ck.store[t] = {"abstract": abstract, "authors_std": a_std,
                                   "affiliations_std": aff_std}
                it.update(1)
                if n_req % SAVE_EVERY_BATCHES == 0:
                    ck.save()

    ck.save()
    print(f"all lookups done ({n_req:,} total requests)")

    ck.backup_parquet(src)
    for col in ["abstract", "authors_std", "affiliations_std"]:
        if col not in df.columns:
            df[col] = ""

    # all three columns are only filled when empty (matching the original script)
    filled = {"abstract": 0, "authors_std": 0, "affiliations_std": 0}
    for idx in tqdm(sub.index, desc="Write back", unit="row", ncols=100):
        rec = ck.store.get(sub.at[idx, "_tn"])
        if not rec:
            continue
        for col in filled:
            if rec.get(col) and not df.at[idx, col]:
                df.at[idx, col] = rec[col]
                filled[col] += 1

    write_back(df, src)
    fill_report(df, "title search", filled)
    return 0


# ====================================================================== stage: citations
def fetch_citations_batch(client: Client, dois: list, with_refs: bool) -> dict:
    """One request for up to 100 DOIs -> {doi: {oa_id, cited_by_count, referenced_works?}}

    `oa_id` is retained because `referenced_works` holds OpenAlex Work IDs of
    the cited papers; knowing this paper's own ID is what allows citation
    relations to be mapped back inside the corpus and mutual citation detected.
    """
    if not dois:
        return {}
    select = "id,doi,cited_by_count" + (",referenced_works" if with_refs else "")
    data = client.get({"filter": "doi:" + "|".join(dois),
                       "per-page": BATCH_DOI, "select": select})
    out = {}
    if not data:
        return out
    for w in data.get("results", []):
        doi = normalize_doi(w.get("doi"))
        if not doi:
            continue
        rec = {"oa_id": (w.get("id") or "").rstrip("/").rsplit("/", 1)[-1],
               "cited_by_count": w.get("cited_by_count")}
        if with_refs:
            rec["referenced_works"] = w.get("referenced_works") or []
        out[doi] = rec
    return out


def load_scope_dois(scope: str, corpus: Path) -> pd.DataFrame:
    """Return the DOIs to fetch for a given scope. doc_id IS the normalized DOI."""
    if scope == "all":
        df = pd.read_parquet(corpus, columns=["doc_id"])
    else:
        df = pd.read_parquet(corpus, columns=["doc_id", "year",
                                              "llama_abm_is_abm",
                                              "llama_abm_methodology"])
        s1 = pd.to_numeric(df["llama_abm_is_abm"], errors="coerce").fillna(0).eq(1)
        if scope == "classic":
            CL = re.compile(r"agent[- ]based|individual[- ]based|micro[- ]?simulat|"
                            r"spatial abm|cellular automat|activity[- ]based", re.I)
            keep = s1 & df["llama_abm_methodology"].astype("string").str.contains(CL, na=False)
        else:  # stage1
            keep = s1
        df = df[keep]
    df = df.copy()
    df["doc_id"] = df["doc_id"].astype(str).map(normalize_doi)
    return df[df["doc_id"].ne("")].drop_duplicates("doc_id")


def stage_citations(args) -> int:
    out_path = Path(args.out)
    print(f"OpenAlex citation fetch | scope={args.scope} | references={args.references}")
    df = load_scope_dois(args.scope, Path(args.corpus))
    print(f"unique DOIs to fetch: {len(df):,}")

    ck = Ckpt(CHECKPOINTS["citations"], "works")
    works = ck.store
    if args.references and ck.data.get("with_refs") is False:
        print("  note: checkpoint holds no references yet; missing fields will be fetched")
    if args.reset:
        n = len(works)
        works.clear()
        ck.data["with_refs"] = False
        print(f"  --reset: cleared {n:,} checkpoint entries (quota will be spent again)")

    all_dois = df["doc_id"].tolist()

    def incomplete(rec: dict) -> bool:
        """Whether a field needed this run is missing.

        - a missing `oa_id` is always re-fetched (required for mutual citation);
        - with --references only `referenced_works is None` counts as missing;
          `[]` means "fetched, genuinely no references" and is not re-requested;
        - unmatched DOIs are stored as `{}` and not re-requested either.
        """
        if not rec:
            return False
        if not rec.get("oa_id"):
            return True
        if args.references and rec.get("referenced_works") is None:
            return True
        return False

    pending = [d for d in all_dois if d not in works or incomplete(works[d])]
    if args.limit:
        pending = pending[: args.limit * BATCH_DOI]
    print(f"pending: {len(pending):,} DOIs -> "
          f"~{max(1, -(-len(pending)//BATCH_DOI)):,} batches")
    if not pending:
        print("nothing pending; writing the summary directly.")

    client = make_client("citations", args.api_key)
    t0 = time.time()
    for i in range(0, len(pending), BATCH_DOI):
        batch = pending[i:i + BATCH_DOI]
        works.update(fetch_citations_batch(client, batch, args.references))
        for d in batch:
            works.setdefault(d, {})
        n_done = i + len(batch)
        if (i // BATCH_DOI + 1) % SAVE_EVERY_BATCHES == 0 or n_done >= len(pending):
            ck.save()
            el = time.time() - t0
            rate = n_done / el if el > 0 else 0
            eta = (len(pending) - n_done) / rate if rate > 0 else 0
            print(f"  {n_done:,}/{len(pending):,}  {rate:6.1f} DOI/s  "
                  f"ETA {eta/60:5.1f} min")

    # mark complete only when every DOI carries references, so a partial state
    # is never mistaken for a finished one
    ck.data["with_refs"] = bool(args.references) and all(
        (not works.get(d)) or (works[d].get("referenced_works") is not None)
        for d in all_dois)
    ck.save()

    hit = {d: v for d, v in works.items() if v}
    print(f"\nOpenAlex hits: {len(hit):,} / {len(df):,} "
          f"({100*len(hit)/max(len(df),1):.1f}%)")

    res = pd.DataFrame([{"doc_id": d, "oa_id": v.get("oa_id"),
                         "oa_cited_by_count": v.get("cited_by_count")}
                        for d, v in hit.items()])
    dup = int(res["doc_id"].duplicated().sum()) if len(res) else 0
    res = res.drop_duplicates("doc_id")
    if dup:
        print(f"  dropped {dup} duplicate rows")
    if args.references:
        refs = {d: v.get("referenced_works") or [] for d, v in hit.items()}
        res["oa_referenced_works"] = res["doc_id"].map(
            lambda d: json.dumps(refs.get(d, []), ensure_ascii=False))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    res.to_parquet(out_path, index=False)
    print(f"saved -> {out_path}  ({len(res):,} rows)")

    compare_with_wos(df, res, works)
    return 0


def compare_with_wos(df: pd.DataFrame, res: pd.DataFrame, works: dict) -> None:
    """Compare against WOS times_cited (only when filtered_final.parquet exists)."""
    fl_path = DB / "filtered_final.parquet"
    if not fl_path.exists():
        return
    fl = pd.read_parquet(fl_path, columns=["doi", "times_cited"])
    fl["doc_id"] = fl["doi"].astype(str).map(normalize_doi)
    fl = fl.drop(columns=["doi"]).drop_duplicates("doc_id")
    m = df.merge(fl, on="doc_id", how="left").merge(res, on="doc_id", how="left")
    # restrict to DOIs actually fetched; otherwise the denominator includes
    # never-fetched rows and the conclusion is meaningless
    attempted = m["doc_id"].isin(works.keys())
    print(f"\n--- comparison with WOS times_cited "
          f"(fetched rows only: {int(attempted.sum()):,}) ---")
    a = m[attempted].copy()
    a["wos"] = pd.to_numeric(a["times_cited"], errors="coerce")
    a["oa"] = pd.to_numeric(a["oa_cited_by_count"], errors="coerce")
    both = a.dropna(subset=["wos", "oa"])
    if len(both) >= 3 and both["wos"].nunique() > 1 and both["oa"].nunique() > 1:
        print(f"  both present: {len(both):,}")
        print(f"  Pearson  r = {both['wos'].corr(both['oa']):.3f}")
        print(f"  Spearman r = {both['wos'].corr(both['oa'], method='spearman'):.3f}")
        print(f"  WOS median {both['wos'].median():.0f} | OA median {both['oa'].median():.0f}")
    else:
        print(f"  both present: {len(both):,} (degenerate sample, skipping correlation)")
    gained = a[a["wos"].fillna(0).eq(0) & a["oa"].notna() & a["oa"].gt(0)]
    print(f"  WOS 0/missing but OpenAlex > 0: {len(gained):,} rows (coverage gap filled)")
    if len(a):
        print(f"  coverage (fetched rows)  WOS>0: {100*a['wos'].fillna(0).gt(0).mean():.1f}%"
              f"  |  OpenAlex>0: {100*a['oa'].fillna(0).gt(0).mean():.1f}%")


# ====================================================================== stage: zc
def stage_zc(args) -> int:
    src = Path(args.file)
    out_path = Path(args.out)
    if not src.exists():
        print(f"[error] {src} not found")
        return 1
    recs = []
    with open(src, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                recs.append(json.loads(line))
    df = pd.DataFrame(recs)
    df["_doi"] = df["doc_id"].apply(normalize_doi)
    has = df["_doi"].ne("")
    print(f"zero-cited records      : {len(df):,}")
    print(f"with a usable DOI       : {int(has.sum()):,}")

    ck = Ckpt(CHECKPOINTS["zc"], "doi")
    dois = df.loc[has, "_doi"].unique().tolist()
    pending = [d for d in dois if d not in ck.store]
    if args.limit:
        pending = pending[:args.limit]
    print(f"already in checkpoint   : {len(dois) - len(pending):,}")
    print(f"pending lookups         : {len(pending):,} -> "
          f"~{max(1, -(-len(pending)//BATCH_DOI)):,} requests\n")

    client = make_client("zc", args.api_key)
    t0 = time.time()
    for i in tqdm(range(0, len(pending), BATCH_DOI),
                  desc="OpenAlex", unit="batch", ncols=100):
        batch = pending[i:i + BATCH_DOI]
        try:
            ck.store.update(lookup_dois_batch(client, batch))
        except Exception as e:
            print(f"\n  [warn] batch failed: {type(e).__name__}: {e}")
        for d in batch:
            ck.store.setdefault(d, {})
        if (i // BATCH_DOI + 1) % SAVE_EVERY_BATCHES == 0:
            ck.save()
    ck.save()
    print(f"lookups done in {time.time() - t0:.0f}s")

    rows = []
    for _, r in df.iterrows():
        rec = ck.store.get(r["_doi"]) or {}
        rows.append({
            "doc_id": r["doc_id"],
            "doi_norm": r["_doi"],
            "authors_std": rec.get("authors_std", ""),
            "affiliations_std": rec.get("affiliations_std", ""),
            "abstract": rec.get("abstract", ""),
        })
    out = pd.DataFrame(rows)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(out_path, index=False)

    a = (out["authors_std"].astype(str).str.strip().ne("")
         & out["authors_std"].astype(str).str.startswith("["))
    print(f"\nauthors_std filled : {int(a.sum()):,} / {len(out):,} ({100*a.mean():.1f} %)")
    print(f"saved              : {out_path}")
    print(f"checkpoint         : {CHECKPOINTS['zc']}")
    return 0


# ====================================================================== CLI
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="enrich.py",
        description="OpenAlex field enrichment (stages: doi / title / citations / zc).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--api-key", default="",
                    help="override the key (priority: this > OPENALEX_API_KEY > stage default)")
    sub = ap.add_subparsers(dest="stage")

    p = sub.add_parser("doi", help="fill abstract / authors_std / affiliations_std by DOI")
    p.add_argument("--file", default=str(DB / "merged_literature.parquet"))
    p.add_argument("--limit", type=int, default=None, help="query only the first N DOIs")

    p = sub.add_parser("title", help="fill the same fields by title search")
    p.add_argument("--file", default=str(DB / "merged_literature.parquet"))
    p.add_argument("--limit", type=int, default=None, help="only the first N titles")

    p = sub.add_parser("citations", help="fetch cited_by_count (optionally referenced_works)")
    p.add_argument("--scope", choices=["classic", "stage1", "all"], default="classic",
                   help="classic = classic ABM subset (default) / stage1 = all stage-1 "
                        "positives / all = the whole corpus")
    p.add_argument("--corpus", default=str(CACHE / "abm_llm_corpus_cache_v3.parquet"))
    p.add_argument("--references", action="store_true",
                   help="also fetch referenced_works (for citation networks)")
    p.add_argument("--limit", type=int, default=None, help="only the first N batches")
    p.add_argument("--reset", action="store_true",
                   help="clear the checkpoint and re-fetch (recommended after re-collection)")
    p.add_argument("--out", default=str(CACHE / "openalex_citations.parquet"))

    p = sub.add_parser("zc", help="author-field enrichment for the zero-cited corpus")
    p.add_argument("--file", default=str(ROOT / "upload" / "zero_cited_corpus.jsonl"))
    p.add_argument("--out", default=str(CACHE / "zc_openalex.parquet"))
    p.add_argument("--limit", type=int, default=None)

    p = sub.add_parser("all", help="run doi -> title -> citations in order")
    p.add_argument("--file", default=str(DB / "merged_literature.parquet"))
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--scope", choices=["classic", "stage1", "all"], default="classic")
    p.add_argument("--corpus", default=str(CACHE / "abm_llm_corpus_cache_v3.parquet"))
    p.add_argument("--references", action="store_true")
    p.add_argument("--reset", action="store_true")
    p.add_argument("--out", default=str(CACHE / "openalex_citations.parquet"))
    return ap


def main() -> int:
    args = build_parser().parse_args()
    stages = {"doi": stage_doi, "title": stage_title,
              "citations": stage_citations, "zc": stage_zc}
    # Default to the DOI stage when executed without arguments (PyCharm: press Run).
    stage = args.stage or "doi"
    if stage == "all":
        for st in ("doi", "title", "citations"):
            print(f"\n{'='*72}\nstage: {st}\n{'='*72}")
            rc = stages[st](args)
            if rc != 0:
                return rc
        return 0
    return stages[stage](args)


if __name__ == "__main__":
    sys.exit(main())
