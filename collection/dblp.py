import gzip
import os
import re
import ssl
import urllib.request

import pandas as pd
from lxml import etree
from tqdm import tqdm


XML_PATH = r"..\database\dblp.xml.gz"
OUTPUT = r"..\database\dblp_agent.parquet"
DBLP_DOWNLOAD_URL = "https://dblp.org/xml/dblp.xml.gz"


# ================= High-precision ABM keyword taxonomy =================

CORE_STEMS = [
    "agent-based",
    "agent based",
    "individual-based",
    "individual based",
    "agent simulation",
    "agent simulations",
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


ALL_KEYWORDS = CORE_STEMS + SOFTWARE_TERMS


def keyword_to_regex(term):
    parts = re.split(r"[\s\-]+", term.strip())
    return r"\b" + r"[- ]".join(re.escape(p) for p in parts) + r"s?\b"


# ================= High-precision ABM phrase patterns =================

CORE_PHRASE_PATTERNS = [
    r"\bagent[- ]based(?:\s+\w+){0,3}\s+(?:model|modeling|modelling|simulation|simulator|framework|approach)s?\b",
    r"\bindividual[- ]based(?:\s+\w+){0,3}\s+(?:model|modeling|modelling|simulation|simulator|framework)s?\b",

    r"\bspatial(?:ly)?\s+agent[- ]based\b",
    r"\bspatially\s+explicit\s+agent[- ]based\b",

    r"\bsugarscape\b",
    r"\bgenerative social science\b",
]


KEYWORD_PATTERNS = [
    keyword_to_regex(term)
    for term in ALL_KEYWORDS
]


POSITIVE_PATTERNS = CORE_PHRASE_PATTERNS + KEYWORD_PATTERNS


# ================= Negative patterns =================

NEGATIVE_PATTERNS = [
    r"\brobot",
    r"\brobotics\b",
    r"\bmulti-robot\b",
    r"\bmultiagent control\b",
    r"\bparticle swarm\b",
    r"\bmobile agent\b",
    r"\bcontrast agent\b",
    r"\bimaging agent\b",
]


# Compile regular expressions for performance
POS_REGEXES = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in POSITIVE_PATTERNS
]

NEG_REGEXES = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in NEGATIVE_PATTERNS
]


class DownloadProgressBar(tqdm):
    # Custom tqdm bar for urllib download updates.
    def update_to(self, b=1, bsize=1, tsize=None):
        if tsize is not None:
            self.total = tsize
        self.update(b * bsize - self.n)


def download_dblp_if_missing(file_path, url, max_retries=5):
    target_dir = os.path.dirname(file_path)

    if target_dir:
        os.makedirs(target_dir, exist_ok=True)

    ssl_context = ssl._create_unverified_context()

    def is_valid_gzip(path):
        if not os.path.exists(path):
            return False

        if os.path.getsize(path) < 2:
            return False

        try:
            with open(path, "rb") as f:
                magic = f.read(2)

            if magic != b"\x1f\x8b":
                return False

            with gzip.open(path, "rb") as f:
                f.read(1024)

            return True

        except Exception:
            return False

    # Validate existing file
    if os.path.exists(file_path):
        if is_valid_gzip(file_path):
            print(f"Found valid DBLP gzip file: {file_path}")
            return

        print(
            f"Invalid DBLP gzip file detected: {file_path}. "
            "Removing it..."
        )

        os.remove(file_path)

    for attempt in range(1, max_retries + 1):

        print(
            f"Starting download from DBLP official source "
            f"(Attempt {attempt}/{max_retries}): {url}"
        )

        temp_path = file_path + ".part"

        if os.path.exists(temp_path):
            os.remove(temp_path)

        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 "
                        "(Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 "
                        "(KHTML, like Gecko) "
                        "Chrome/154.0 Safari/537.36"
                    ),
                    "Accept": "application/gzip, application/octet-stream, */*",
                },
            )

            with urllib.request.urlopen(
                req,
                context=ssl_context,
                timeout=120,
            ) as response:

                status_code = response.getcode()
                content_type = response.headers.get(
                    "Content-Type",
                    "",
                ).lower()

                print(f"HTTP status: {status_code}")
                print(f"Content-Type: {content_type}")

                if status_code != 200:
                    raise RuntimeError(
                        f"HTTP request failed with status {status_code}"
                    )

                with open(temp_path, "wb") as out_file:

                    first_chunk = response.read(1024)

                    if not first_chunk:
                        raise RuntimeError(
                            "Server returned an empty response."
                        )

                    # gzip magic bytes
                    if not first_chunk.startswith(b"\x1f\x8b"):
                        preview = first_chunk[:100]

                        raise RuntimeError(
                            "Downloaded content is not gzip data. "
                            f"First bytes: {preview!r}"
                        )

                    out_file.write(first_chunk)

                    with DownloadProgressBar(
                        unit="B",
                        unit_scale=True,
                        miniters=1,
                        desc="Downloading dblp.xml.gz",
                    ) as progress:

                        progress.update(len(first_chunk))

                        while True:
                            buffer = response.read(1024 * 1024)

                            if not buffer:
                                break

                            out_file.write(buffer)
                            progress.update(len(buffer))

            # Validate downloaded gzip
            if not is_valid_gzip(temp_path):
                raise RuntimeError(
                    "Downloaded file failed gzip validation."
                )

            # Replace old file only after validation succeeds
            if os.path.exists(file_path):
                os.remove(file_path)

            os.replace(temp_path, file_path)

            size_mb = os.path.getsize(file_path) / 1024 / 1024

            print(
                f"\nDownload completed successfully! "
                f"Valid gzip size: {size_mb:.2f} MB"
            )

            return

        except Exception as e:

            print(
                f"\nDownload failed on attempt "
                f"{attempt}/{max_retries}: {e}"
            )

            if os.path.exists(temp_path):
                os.remove(temp_path)

            if attempt == max_retries:
                raise RuntimeError(
                    "Failed to download a valid DBLP gzip file "
                    f"after {max_retries} attempts."
                )


def match_title(title):
    # Match title against positive patterns while filtering negative patterns.
    if not title:
        return False, []

    # Check negative filters first
    for neg_re in NEG_REGEXES:
        if neg_re.search(title):
            return False, []

    # Match positive keywords
    matched_tags = []

    for pos_re in POS_REGEXES:
        match = pos_re.search(title)

        if match:
            matched_tags.append(
                match.group(0).lower()
            )

    if matched_tags:
        return True, list(set(matched_tags))

    return False, []


def parse_dblp():
    # Parse DBLP XML file and extract articles matching ABM keywords.
    papers = []

    with gzip.open(XML_PATH, "rb") as f:

        context = etree.iterparse(
            f,
            events=("end",),
            tag=(
                "article",
                "inproceedings",
                "incollection",
                "phdthesis",
            ),
            recover=True,
        )

        for _, elem in tqdm(
            context,
            desc="Parsing DBLP",
        ):

            title = elem.findtext("title")

            is_match, matched_kw = match_title(title)

            if is_match:

                authors = [
                    a.text
                    for a in elem.findall("author")
                    if a.text
                ]

                affiliations = []

                for a in elem.findall("author"):
                    affil = (
                        a.get("aux")
                        or a.get("orcid")
                        or a.findtext("address")
                    )

                    if affil:
                        affiliations.append(affil)

                xml_keywords = [
                    kw.text
                    for kw in elem.findall("keyword")
                    if kw.text
                ]

                all_keywords = list(
                    set(matched_kw + xml_keywords)
                )

                year = elem.findtext("year")
                journal = elem.findtext("journal")
                venue = elem.findtext("booktitle")

                doi = ""

                for ee in elem.findall("ee"):
                    if (
                        ee.text
                        and "doi.org" in ee.text
                    ):
                        doi = ee.text
                        break

                papers.append(
                    {
                        "title": title,
                        "authors": authors,
                        "affiliations": affiliations,
                        "keywords": all_keywords,
                        "year": year,
                        "journal": journal,
                        "venue": venue,
                        "doi": doi,
                        "type": elem.tag,
                        "source": "DBLP",
                    }
                )

            elem.clear()

            while elem.getprevious() is not None:
                del elem.getparent()[0]

    return papers


if __name__ == "__main__":

    # Ensure file exists and is completely downloaded before parsing
    download_dblp_if_missing(
        XML_PATH,
        DBLP_DOWNLOAD_URL,
    )

    print("File:", XML_PATH)
    print(
        "Size:",
        round(
            os.path.getsize(XML_PATH) / 1024 / 1024,
            2,
        ),
        "MB",
    )

    data = parse_dblp()

    print("Matched papers:", len(data))

    df = pd.DataFrame(data)

    df.to_parquet(
        OUTPUT,
        index=False,
    )

    print("Saved:", OUTPUT)