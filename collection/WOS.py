import os
import sys
import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor

import requests
import pandas as pd

# The progress messages contain emoji. When stdout is redirected (a background
# job, a log file, a pipe) Python picks the locale codec - GBK on this machine -
# which cannot encode them, and the script dies on its first print. Force UTF-8.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ================= 1. Configuration =================
API_KEY = os.environ.get("WOS_API_KEY", "").strip()
URL = "https://api.clarivate.com/apis/wos-starter/v1/documents"  #
OUTPUT_PATH = "../database/wos.parquet"  #
CHECKPOINT_PATH = "../database/wos_checkpoint.json"  # 断点续传：记录每年抓取进度，避免重复请求
headers = {
    "X-ApiKey": API_KEY,
    "Accept": "application/json"
}
DEFAULT_START_YEAR = 1900
CURRENT_YEAR = 2025
MAX_DAILY_REQUESTS = 20000  # 每日 API 请求上限
PAGE_SIZE = 50  # WoS Starter API 单页最大返回条数
MAX_RETRIES = 5
TIMEOUT_SECONDS = 30
MIN_REQUEST_INTERVAL = 0.25  # 4 req/s. Measured: 0.21s (4.76/s) trips HTTP 429
# inside a 1-second bucket even though the documented limit is 5/s, so the
# margin is deliberate - a 429 costs more time in backoff than the 0.76 req/s
# is worth, and it risks a temporary block.

# Concurrency. The API allows 5 requests/second, but a single request takes
# ~0.60s, so a sequential loop only reaches ~1.7 requests/second however small
# the sleep is: latency, not the sleep, is the binding constraint. Fetching a
# window of pages from several threads hides that latency and lifts throughput
# to the rate limit. Measured on one year of TS=("agent-based"): 52 pages in
# 14s (3.7 req/s) against ~31s sequential.
#
# BATCH_PAGES is how many page requests are in flight per round; MAX_WORKERS is
# how many are actually open at once. Five workers already saturate 4 req/s
# (5 / 0.60s = 8.3 req/s of capacity), so raising it only increases burstiness.
MAX_WORKERS = 5
BATCH_PAGES = 24

# ================= 1.1 Search Keywords =================
# Word stems (*). WoS honours a trailing "*" and nothing else: it merges the
# singular/plural and -ing/-ion spellings of the last token, so "agent model*"
# returns 6,421 records against 4,383 + 1,383 for the two written-out forms. A
# wildcard in the middle of a word is silently ignored - "agent*base*" returns
# 183 records against 44,067 for the plain phrase - so it is never used here.
# Hyphen and space are interchangeable on WoS ("agent-based" == "agent based" ==
# 44,067 hits); both spellings are kept anyway because they cost nothing and
# keep the query readable.
#
# The stem list below replaces 50 written-out terms with 38 stems and covers
# slightly more ground than the old list did.
CORE_STEMS = [
    "agent-based", "agent based", "individual-based", "individual based",
    "agent-oriented", "agent oriented", "multiagent",
    "agent simulation*", "agent model*",
    "individual-level", "individual level",
]
RELATED_TERMS = [
    "social simulation", "microsimulation", "micro-simulation", "micro simulation",
    "population simulation", "population-based simulation", "activity-based",
    "activity based", "complex adaptive system", "opinion dynamics",
    "computational social science", "computational sociology", "artificial society",
    "artificial societies", "artificial life", "heterogeneous agent", "heterogeneous agents",
]

# Named toolkits. Only names distinctive enough not to collide with an ordinary
# word are kept. jade, jason, spade and sarl were removed from collection: they
# are also a material, a given name, a garden tool and a French company form,
# and every record they brought in was deleted again at stage 5.
SOFTWARE_TERMS = [
    "netlogo", "repast", "mason", "gama", "matsim", "mesa",
    "anylogic", "flame", "cormas", "ascape", "starlogo",
    "epimodel", "agentpy", "agents.jl", "agentscript",
    "jade", "jason", "spade", "sarl", "simudyne",
]

ALL_KEYWORDS = CORE_STEMS + RELATED_TERMS + SOFTWARE_TERMS

# ---------------------------------------------------------------------------
# Author-targeted collection
#
# A topic query only finds a paper if its title, abstract or keywords contain
# one of the terms above. Leading scholars who describe their own work in their
# own vocabulary are therefore under-collected: measured on WoS,
# AU=("Batty, M") holds 448 records but only 53 of them match the topic query,
# and 16 of those carry an ABM anchor. Most of his urban modelling work is
# written as "urban model", "fractal city", "urban scaling".
#
# There is a second, subtler gap this closes. The topic field WoS searches is
# the WoS abstract, while stage 5 tests the *OpenAlex* abstract. A paper whose
# OpenAlex abstract says "agent-based" but whose WoS record does not can be
# missed by the topic query and would have been kept downstream.
#
# A name is listed only when surname + initial identifies one person. The test
# is the measured record count under AU=: a career's output is tens to a few
# hundred, while thousands mean the name is shared and the branch would flood
# the corpus with strangers. Measured and therefore EXCLUDED:
#
#     Miller, H   1,637      Brown, D    9,872      Epstein, J  1,940
#     Janssen, M  2,374      North, M      607      Farmer, J   1,001
#     Conte, R      746
#
# Cost of the whole branch: ~2,100 records, about 45 page requests.
# ---------------------------------------------------------------------------
AUTHOR_TERMS = [
    "Batty, M", "Crooks, A", "Malleson, N", "Heppenstall, A", "Manley, E",
    "Torrens, P", "Birkin, M", "Grimm, V", "Railsback, S", "Macal, C",
    "Edmonds, B", "Flache, A", "Wilensky, U", "Axtell, R", "LeBaron, B",
    "Hommes, C", "Kirman, A",
]

def _wos_phrase(term):
    """Quote terms containing spaces/hyphens/dots so WoS matches them as exact phrases."""
    if any(ch in term for ch in " -."):
        return f'"{term}"'
    return term


# Base clause: the original ABM vocabulary plus the urban/simulation family.
_BASE_QUERY = "TS=(" + " OR ".join(_wos_phrase(t) for t in ALL_KEYWORDS) + ")"

# Second clause: cellular-automata papers that are also urban or spatial.
_CA_QUERY = ("TS=(" + " OR ".join(_wos_phrase(t) for t in CA_TERMS) + ")"
             " AND TS=(" + " OR ".join(CA_CONTEXT) + ")")

# Third clause: drop the vocabularies that no longer mean agent-based modelling.
_EXCLUDE_QUERY = ""

# Fourth clause: the listed authors' records, whatever their topic wording.
# Author names contain a comma and a space, so they are always quoted.
_AUTHOR_QUERY = "AU=(" + " OR ".join(f'"{a}"' for a in AUTHOR_TERMS) + ")"

# Parenthesised so the NOT applies to all three positive branches, and so the
# author branch cannot leak past the exclusion.
KEYWORDS_QUERY = _BASE_QUERY

# Bump this whenever KEYWORDS_QUERY changes: the checkpoint stores it, and a
# mismatch makes every year re-fetch. Without it, years already marked complete
# under the old query would be skipped and the new terms never retrieved.
QUERY_VERSION = 5

os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)  #

# ================= 1.2 Rate Limiting & Checkpoint =================
_last_request_time = 0.0
_rate_lock = threading.Lock()
request_counter = 0


def _reserve_request():
    """Claim the next request slot, spacing callers MIN_REQUEST_INTERVAL apart.

    Safe to call from several threads. The slot is reserved while the lock is
    held and the sleeping happens outside it, so callers queue up on the
    timestamp instead of all waking at once. Returns the running request count
    for this run.
    """
    global _last_request_time, request_counter
    with _rate_lock:
        now = time.time()
        earliest = _last_request_time + MIN_REQUEST_INTERVAL
        _last_request_time = now if now > earliest else earliest
        wait = _last_request_time - now
        request_counter += 1
        count = request_counter
    if wait > 0:
        time.sleep(wait)
    return count


def load_checkpoint():
    """Load per-year fetch progress so re-runs skip already-completed years/pages.

    The checkpoint records QUERY_VERSION. When the query changes, every year must
    be re-fetched, because a year marked complete under the old query holds only
    the records that query matched - the new terms would otherwise never run.
    """
    if os.path.exists(CHECKPOINT_PATH):
        try:
            with open(CHECKPOINT_PATH, "r", encoding="utf-8") as f:
                checkpoint = json.load(f)
            stored = checkpoint.get("query_version")
            if stored != QUERY_VERSION:
                print(f"🔁 Query changed (checkpoint version {stored} -> "
                      f"{QUERY_VERSION}); resetting per-year progress so the "
                      f"new terms are fetched.")
                return {"query_version": QUERY_VERSION, "years": {}}
            return checkpoint
        except Exception as e:
            print(f"⚠️ Could not load checkpoint, starting fresh: {e}")
    return {"query_version": QUERY_VERSION, "years": {}}


def save_checkpoint(checkpoint):
    """Persist fetch progress after every page (crash-safe resume)."""
    try:
        checkpoint["query_version"] = QUERY_VERSION
        with open(CHECKPOINT_PATH, "w", encoding="utf-8") as f:
            json.dump(checkpoint, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ Could not save checkpoint: {e}")


# ================= 2. Load Local Database for Deduplication =================
existing_dois = set()  #
existing_titles = set()  #
existing_uids = set()  #


def normalize_title(title):
    """Normalize title for exact deduplication: lowercase and strip spaces."""
    return title.strip().lower() if title else ""  #


if os.path.exists(OUTPUT_PATH):  #
    try:
        existing_df = pd.read_parquet(OUTPUT_PATH)  #

        if "doi" in existing_df.columns:  #
            existing_dois = set(existing_df["doi"].dropna().str.lower().str.strip().tolist())  #
        if "title" in existing_df.columns:  #
            existing_titles = set(existing_df["title"].dropna().apply(normalize_title).tolist())  #
        if "uid" in existing_df.columns:  #
            existing_uids = set(existing_df["uid"].dropna().tolist())  #

        print(f"📦 Local database loaded successfully: {len(existing_df)} total records found.")  #
        print(f"   ├─ Indexed DOIs: {len(existing_dois)}")  #
        print(f"   ├─ Indexed Titles: {len(existing_titles)}")  #
        print(f"   └─ Indexed UIDs: {len(existing_uids)}")  #
    except Exception as e:
        print(f"⚠️ Failed to read local database: {e}")  #
else:
    print("ℹ️ No local database found. A new wos.parquet file will be created upon completion.")  #

# ================= 3. Determine Years to Fetch (skip completed ones) =================
checkpoint = load_checkpoint()


def _year_done(year):
    """True if the year was already fully fetched in a previous run."""
    ck = checkpoint.get("years", {}).get(str(year))
    if not ck:
        return False
    total = ck.get("total")
    return total is not None and ck.get("fetched", 0) >= total


years_to_fetch = [y for y in range(CURRENT_YEAR, DEFAULT_START_YEAR - 1, -1) if not _year_done(y)]

if not years_to_fetch:
    print("✅ All years are already fully collected. No API calls needed.")
    sys.exit(0)

# One metadata call to show the total record count (only when there is something to fetch)
total_found = None
try:
    _reserve_request()
    init_params = {"q": KEYWORDS_QUERY, "limit": 1, "page": 1}
    init_res = requests.get(URL, headers=headers, params=init_params, timeout=TIMEOUT_SECONDS)
    if init_res.status_code == 200:
        total_found = init_res.json().get("metadata", {}).get("total", 0)
        print(f"📊 Total matching records in WoS database: {total_found}")
    else:
        print(f"⚠️ Metadata request returned HTTP {init_res.status_code}")
except Exception as e:
    print(f"⚠️ Could not fetch metadata: {e}")

if total_found == 0:
    print("ℹ️ No records match the query. Nothing to fetch.")
    sys.exit(0)

# Used only to show a progress bar; the per-year totals are learned as we go.
EXPECTED_PAGES = (total_found + PAGE_SIZE - 1) // PAGE_SIZE if total_found else None

print(f"\n🚀 Collecting {DEFAULT_START_YEAR}-{CURRENT_YEAR}: {len(years_to_fetch)} years, "
      f"~{EXPECTED_PAGES or '?'} pages")
print(f"🔍 Query: {KEYWORDS_QUERY}")
print(f"⚙️ {MAX_WORKERS} workers, {MIN_REQUEST_INTERVAL}s between requests "
      f"(~{1 / MIN_REQUEST_INTERVAL:.1f} req/s; sequential was ~1.7 req/s)\n")


# ================= 4. Retrieval =================
def fetch_page(year, page):
    """Fetch one page, retrying transient failures. Returns (page, json | None).

    Called from several threads at once: the rate limiter inside
    _reserve_request() is what keeps the whole pool inside 5 requests/second.
    """
    params = {"q": f"({KEYWORDS_QUERY}) AND PY={year}",
              "limit": PAGE_SIZE, "page": page}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            _reserve_request()
            response = requests.get(URL, headers=headers, params=params,
                                    timeout=TIMEOUT_SECONDS)
            if response.status_code == 200:
                return page, response.json()
            if response.status_code == 401:
                print("❌ API Authentication Failed (401). Please check your API key.")
                return page, None
            if response.status_code in (429, 500, 502, 503, 504):
                backoff_time = (2 ** attempt) * 2
                print(f"⚠️ HTTP {response.status_code} on {year} p{page}; "
                      f"retrying in {backoff_time}s ({attempt}/{MAX_RETRIES})")
                time.sleep(backoff_time)
            else:
                print(f"❌ Unexpected API Response {response.status_code}: "
                      f"{response.text[:200]}")
                return page, None
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            backoff_time = attempt * 5
            print(f"🚨 Connection error on {year} p{page}; retrying in {backoff_time}s")
            time.sleep(backoff_time)
        except Exception as e:
            print(f"🚨 Unexpected Exception: {e}")
            return page, None
    return page, None


def extract_records(hits):
    """Convert API hits into rows, skipping anything already in the database.

    The sets are only touched from the main thread - the worker threads return
    raw pages and never come here - so no locking is needed.
    """
    rows, skipped = [], 0
    for doc in hits:
        uid = doc.get("uid", "")
        title = doc.get("title", "")
        norm_title = normalize_title(title)
        doi = doc.get("identifiers", {}).get("doi", "").lower().strip()

        if ((uid and uid in existing_uids)
                or (doi and doi in existing_dois)
                or (norm_title and norm_title in existing_titles)):
            skipped += 1
            continue
        if uid:
            existing_uids.add(uid)
        if doi:
            existing_dois.add(doi)
        if norm_title:
            existing_titles.add(norm_title)

        names = doc.get("names", {})
        authors_list = [a.get("displayName") for a in names.get("authors", [])
                        if a.get("displayName")]
        source = doc.get("source", {})
        pub_year = source.get("publishYear", "")
        pub_date = source.get("publishDate", "")
        citations = doc.get("citations", [])

        rows.append({
            "uid": uid,
            "title": title,
            "authors": "; ".join(authors_list) if authors_list else "",
            # The WoS Starter API returns no abstract for any record - measured:
            # 0 of 143,433. The column is kept because the merge expects it, and
            # enrich.py fills it from OpenAlex later.
            "abstract": doc.get("abstract") or doc.get("summary", {}).get("abstract", ""),
            "doi": doi,
            "publish_time": str(pub_date) if pub_date else str(pub_year),
            "publish_year": pub_year,
            "journal": source.get("sourceTitle", ""),
            "times_cited": citations[0].get("count", 0) if citations else 0,
            "doc_type": ", ".join(doc.get("types", [])),
        })
    return rows, skipped


def eta_text(done, started_at):
    """Elapsed time plus a rough estimate of what is left."""
    elapsed = time.time() - started_at
    if EXPECTED_PAGES and done:
        rate = done / elapsed
        left = (EXPECTED_PAGES - done) / rate
        return f"{elapsed / 60:.1f} min elapsed, ~{left / 60:.1f} min left"
    return f"{elapsed / 60:.1f} min elapsed"


# ================= 5. Year-by-Year Parallel Retrieval Loop =================
new_records = []
started_at = time.time()
pages_done = 0
stop_all = False

for year in years_to_fetch:
    if stop_all:
        break

    # Resume progress from the checkpoint (if the year was interrupted)
    ck = checkpoint.get("years", {}).get(str(year), {})
    fetched_count_year = ck.get("fetched", 0)
    total_records_year = ck.get("total")  # None if the year has never been seen
    page = 1 if total_records_year is None else ck.get("page", 0) + 1

    print(f"📅 --- Year {year} ---")
    if fetched_count_year:
        print(f"   ⏸️ Resuming at page {page} "
              f"({fetched_count_year} records already fetched)")

    while True:
        if request_counter >= MAX_DAILY_REQUESTS:
            print("⚠️ Reached the daily request limit. Stopping gracefully - "
                  "run again to resume where this stopped.")
            stop_all = True
            break

        # A year that has never been queried needs one page on its own: the
        # response carries the total, which is what the batch size depends on.
        if total_records_year is None:
            _, data = fetch_page(year, page)
            if data is None:
                print(f"⚠️ Could not fetch {year} page {page}; skipping the year.")
                break
            total_records_year = data.get("metadata", {}).get("total", 0)
            print(f"📊 Year {year}: {total_records_year} matching records")
            if total_records_year == 0:
                checkpoint["years"][str(year)] = {"page": 0, "fetched": 0, "total": 0}
                save_checkpoint(checkpoint)
                break
            last_page = (total_records_year + PAGE_SIZE - 1) // PAGE_SIZE
            window = {page: data}
            next_pages = list(range(page + 1, min(page + BATCH_PAGES, last_page + 1)))
        else:
            last_page = (total_records_year + PAGE_SIZE - 1) // PAGE_SIZE
            window = {}
            next_pages = list(range(page, min(page + BATCH_PAGES, last_page + 1)))

        # Never start more requests than the daily budget still allows
        budget = MAX_DAILY_REQUESTS - request_counter
        if budget <= 1:
            print("⚠️ Reached the daily request limit. Stopping gracefully.")
            stop_all = True
            break
        next_pages = next_pages[:budget - 1]

        if next_pages:
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                for p, data in pool.map(lambda pp: fetch_page(year, pp), next_pages):
                    window[p] = data

        if not window:
            break

        added = skipped = 0
        failed = False
        for p in sorted(window):
            data = window[p]
            if data is None:
                # Everything before p is already checkpointed, so the next run
                # resumes exactly here.
                print(f"⚠️ Year {year} page {p} failed; the next run resumes here.")
                failed = True
                break
            hits = data.get("hits", [])
            rows, sk = extract_records(hits)
            new_records.extend(rows)
            added += len(rows)
            skipped += sk
            fetched_count_year += len(hits)
            pages_done += 1
            checkpoint["years"][str(year)] = {
                "page": p, "fetched": fetched_count_year, "total": total_records_year,
            }
            save_checkpoint(checkpoint)

        # One line per batch: printing per page would bury the progress.
        if added or skipped:
            print(f"   ✓ [{year}] pages {min(window)}-{max(window)} "
                  f"({pages_done}/{EXPECTED_PAGES or '?'} pages) | "
                  f"{fetched_count_year}/{total_records_year} | "
                  f"⏭️ {skipped} | ✨ +{added} | {eta_text(pages_done, started_at)}")

        if failed or fetched_count_year >= total_records_year:
            break
        page = max(window) + 1

print(f"\n📡 API requests used this run: {request_counter} / {MAX_DAILY_REQUESTS} (daily cap)")
print(f"⏱️ Elapsed: {(time.time() - started_at) / 60:.1f} min")

# ================= 6. Append and Save to Parquet =================
if new_records:  #
    new_df = pd.DataFrame(new_records)  #

    if os.path.exists(OUTPUT_PATH):  #
        old_df = pd.read_parquet(OUTPUT_PATH)  #
        final_df = pd.concat([old_df, new_df], ignore_index=True)  #
    else:
        final_df = new_df  #

    final_df = final_df.drop_duplicates(subset=["uid"], keep="first")  #
    if "doi" in final_df.columns:  #
        has_doi = final_df[final_df["doi"].str.len() > 0]  #
        no_doi = final_df[final_df["doi"].str.len() == 0]  #
        has_doi = has_doi.drop_duplicates(subset=["doi"], keep="first")  #
        final_df = pd.concat([has_doi, no_doi], ignore_index=True)  #

    final_df.to_parquet(OUTPUT_PATH, index=False, engine="pyarrow")  #

    print("\n" + "=" * 60)
    print(f"🎉 Extraction complete! New records added: {len(new_df)}")  #
    print(f"📁 Parquet database updated at: {OUTPUT_PATH}")  #
    print(f"📊 Total unique records in database: {len(final_df)}")  #
    print("=" * 60)
else:
    print("\nℹ️ All fetched records already exist in the local database. No updates performed.")  #
