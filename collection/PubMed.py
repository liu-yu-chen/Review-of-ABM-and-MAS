import json
import os
import time
from datetime import datetime
import pandas as pd
from Bio import Entrez
from tqdm import tqdm

# Entrez configuration
Entrez.email = os.environ.get("NCBI_EMAIL", "your.email@example.com").strip()
Entrez.tool = "MyPubMedScript"
Entrez.api_key = os.environ.get("NCBI_API_KEY", "").strip()

# Path configuration
OUTPUT_FILE = "../database/pubmed.parquet"
CHECKPOINT_FILE = "../database/pubmed_checkpoint.json"
TEMP_DIR = "../database/pubmed_temp"

# NCBI E-utilities: with an API key the limit is 10 requests/second.
# 0.11s between requests (~9 req/s) keeps us under that cap while being fast.
MIN_REQUEST_INTERVAL = 0.11
BATCH_SIZE = 1000  # efetch supports up to 10,000; 1,000 cuts request count without huge payloads

os.makedirs("../database", exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

_last_request_time = 0.0


def _rate_limit():
    # Global rate limiter: ensure at least MIN_REQUEST_INTERVAL between every API call.
    global _last_request_time
    now = time.time()
    elapsed = now - _last_request_time
    if _last_request_time > 0 and elapsed < MIN_REQUEST_INTERVAL:
        time.sleep(MIN_REQUEST_INTERVAL - elapsed)
    _last_request_time = time.time()


def load_existing_dois():
    # Load existing DOIs from the parquet file to avoid redundant downloads.
    if not os.path.exists(OUTPUT_FILE):
        return set()

    try:
        df = pd.read_parquet(OUTPUT_FILE)
        if "doi" in df.columns:
            return set(df["doi"].dropna().astype(str).tolist())
    except Exception as e:
        print("Load parquet error:", e)

    return set()


def search_pubmed_with_history(query_string):
    # Execute search on PubMed using WebEnv history server.
    try:
        _rate_limit()
        with Entrez.esearch(
            db="pubmed",
            term=query_string,
            retmax=0,
            usehistory="y",
            sort="relevance",
        ) as handle:
            result = Entrez.read(handle)
            return (
                result.get("WebEnv"),
                result.get("QueryKey"),
                int(result.get("Count", 0)),
            )
    except Exception as e:
        print("Search error:", e)
        return None, None, 0


def fetch_pubmed_batch(webenv, query_key, retstart, retmax, retries=3):
    # Fetch article records in batches from PubMed.
    for i in range(retries):
        try:
            _rate_limit()
            with Entrez.efetch(
                db="pubmed",
                rettype="xml",
                retmode="xml",
                retstart=retstart,
                retmax=retmax,
                webenv=webenv,
                query_key=query_key,
            ) as handle:
                data = Entrez.read(handle)
                return data.get("PubmedArticle", [])
        except Exception as e:
            print(f"Fetch error at retstart {retstart} (retry {i+1}):", e)
            time.sleep(5)

    return []


def parse_article(article):
    # Parse XML article record into a flat dictionary structure.
    try:
        medline = article["MedlineCitation"]
        art = medline["Article"]

        title = str(art.get("ArticleTitle", ""))

        abstract = ""
        if art.get("Abstract"):
            texts = []
            for x in art["Abstract"]["AbstractText"]:
                if hasattr(x, "attributes") and "Label" in x.attributes:
                    texts.append(x.attributes["Label"] + ": " + str(x))
                else:
                    texts.append(str(x))
            abstract = " ".join(texts)

        authors = []
        affiliations = []

        for a in art.get("AuthorList", []):
            name = (a.get("LastName", "") + " " + a.get("ForeName", "")).strip()
            if name:
                authors.append(name)

            for aff in a.get("AffiliationInfo", []):
                txt = str(aff.get("Affiliation", "")).strip()
                if txt:
                    affiliations.append(txt)

        journal = str(art.get("Journal", {}).get("Title", ""))

        year = ""
        try:
            year = str(
                art["Journal"]["JournalIssue"]["PubDate"].get("Year", "")
            )
        except Exception:
            pass

        doi = ""
        for item in article.get("PubmedData", {}).get("ArticleIdList", []):
            if item.attributes.get("IdType") == "doi":
                doi = str(item)
                break

        keywords = []
        for klist in medline.get("KeywordList", []):
            for k in klist:
                keywords.append(str(k))

        mesh = []
        for m in medline.get("MeshHeadingList", []):
            mesh.append(str(m["DescriptorName"]))

        return {
            "pmid": str(medline["PMID"]),
            "title": title,
            "abstract": abstract,
            "core_text": "Title: " + title + "\nAbstract: " + abstract,
            "authors": json.dumps(authors, ensure_ascii=False),
            "affiliations": json.dumps(
                list(set(affiliations)), ensure_ascii=False
            ),
            "journal": journal,
            "year": year,
            "doi": doi,
            "keywords": json.dumps(keywords, ensure_ascii=False),
            "mesh_terms": json.dumps(mesh, ensure_ascii=False),
            "source": "PubMed",
        }

    except Exception as e:
        print("Parse error:", e)
        return None


def save_batch_parquet(batch, file_tag):
    # Save parsed batch result to a temporary parquet file.
    if not batch:
        return

    df = pd.DataFrame(batch)
    path = os.path.join(TEMP_DIR, f"batch_{file_tag}.parquet")
    df.to_parquet(path, index=False)


def merge_parquet():
    # Merge all temporary batch files with existing dataset and deduplicate by DOI.
    files = [
        os.path.join(TEMP_DIR, x)
        for x in os.listdir(TEMP_DIR)
        if x.endswith(".parquet")
    ]

    if not files:
        return

    dfs = []
    if os.path.exists(OUTPUT_FILE):
        dfs.append(pd.read_parquet(OUTPUT_FILE))

    for f in files:
        dfs.append(pd.read_parquet(f))

    df = pd.concat(dfs, ignore_index=True)

    if "doi" in df.columns:
        df = df.drop_duplicates(subset=["doi"], keep="first")

    df.to_parquet(OUTPUT_FILE, index=False)
    print("Merged and saved total papers:", len(df))


def load_checkpoint():
    # Load checkpoint for resume capability.
    if os.path.exists(CHECKPOINT_FILE):
        return json.load(open(CHECKPOINT_FILE, encoding="utf-8"))
    return {}


def save_checkpoint(data):
    # Save current progress checkpoint.
    json.dump(data, open(CHECKPOINT_FILE, "w", encoding="utf-8"), indent=2)


# ================= 检索关键词：ABM + 泛ABM（与 WOS.py / openalex.py 同源）=================
CORE_STEMS = [
    "agent-based",
    "agent based",
    "individual-based",
    "individual based",
    "agent simulation",
    "agent simulations",
]

RELATED_TERMS = [
    "heterogeneous agent",
    "heterogeneous agents",
]

SOFTWARE_TERMS = [
    "netlogo",
    "repast",
    "mason",
    "gama",
    "matsim",
    "mesa",
    "anylogic",
    "cormas",
    "ascape",
    "starlogo",
    "epimodel",
    "agentpy",
    "agents.jl",
    "agentscript",
    "simudyne",
]

ALL_KEYWORDS = CORE_STEMS + RELATED_TERMS + SOFTWARE_TERMS

# 排除文献类型（沿用原逻辑）
EXCLUDE_TYPES = [
    "Editorial",
    "Letter",
    "Book Review",
    "Erratum",
    "Short Communication",
    "Comment",
    "Response",
    "Correction",
    "Notes",
]

# 排除明显误检词（robot、对比剂/成像剂等）；不排除 multi-agent/multiagent，保证泛ABM（含 MAS）在检索范围内
EXCLUDE_TERMS = [
    "robot*[Title/Abstract]",
    "robotics[MeSH Terms]",
    '"multi-agent control"[Title/Abstract]',
    '"contrast agent*"[Title/Abstract]',
    '"imaging agent*"[Title/Abstract]',
]


def _pubmed_phrase(term):
    # Quote phrases (space/hyphen/dot) so PubMed matches them as exact phrases.
    if any(ch in term for ch in " -."):
        return f'"{term}"'
    return term


def build_base_query():
    # Build the PubMed query from ALL_KEYWORDS + exclusions.
    abm_keywords = [_pubmed_phrase(t) + "[Title/Abstract]" for t in ALL_KEYWORDS]
    pt_exclude = "NOT (" + " OR ".join([f"{x}[pt]" for x in EXCLUDE_TYPES]) + ")"
    term_exclude = "NOT (" + " OR ".join(EXCLUDE_TERMS) + ")"
    return (
        "("
        + " OR ".join(abm_keywords)
        + ") "
        + pt_exclude
        + " "
        + term_exclude
    )


if __name__ == "__main__":

    existing_dois = load_existing_dois()
    print("Existing DOI count:", len(existing_dois))

    base_query = build_base_query()
    print("PubMed Base Query:")
    print(base_query)

    checkpoint = load_checkpoint()

    # Define years chunk to process (e.g. from 1950 to current year)
    current_year = datetime.now().year
    years = list(range(1950, current_year + 1))

    # ===== Pass 1: quick esearch per year to get hit counts (no record download) =====
    # WebEnv/QueryKey are valid for 8 hours, so we reuse them in pass 2 (no extra requests).
    year_meta = {}
    for year in years:
        year_query = f"({base_query}) AND ({year}[dp])"
        webenv, key, total = search_pubmed_with_history(year_query)
        year_meta[year] = (webenv, key, total)
        if total > 9999:
            print(
                f"⚠️ Warning: Year {year} has {total} records (>9999), "
                f"consider splitting by month."
            )

    # Compute how many records still need fetching (respecting checkpoint resume)
    total_to_fetch = 0
    for year, (webenv, key, total) in year_meta.items():
        if total == 0:
            continue
        start = checkpoint.get(f"year_{year}", 0)
        if start >= total:
            continue
        total_to_fetch += total - start

    print(f"Total records to fetch: {total_to_fetch} across {len(years)} years")

    # ===== Pass 2: single progress bar over the total record count =====
    if total_to_fetch > 0:
        with tqdm(total=total_to_fetch, desc="Fetching PubMed", unit="rec", ncols=100) as bar:
            for year, (webenv, key, total) in year_meta.items():
                if total == 0:
                    continue
                year_key = f"year_{year}"
                start = checkpoint.get(year_key, 0)
                if start >= total:
                    continue

                bar.set_description(f"Fetching PubMed | {year} ({total} hits)")

                for retstart in range(start, total, BATCH_SIZE):
                    records = fetch_pubmed_batch(webenv, key, retstart, BATCH_SIZE)

                    if not records:
                        break

                    batch = []
                    for r in records:
                        p = parse_article(r)
                        if p:
                            doi = p["doi"]
                            if not doi or doi not in existing_dois:
                                batch.append(p)
                                if doi:
                                    existing_dois.add(doi)

                    file_tag = f"{year}_{retstart}"
                    save_batch_parquet(batch, file_tag)

                    checkpoint[year_key] = retstart + len(records)
                    save_checkpoint(checkpoint)

                    bar.update(len(records))
    else:
        print("All years already completed. Nothing to fetch.")

    merge_parquet()
    print("Process Finished Successfully!")
