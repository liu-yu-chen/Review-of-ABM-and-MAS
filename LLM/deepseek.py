#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ABM literature classification with DeepSeek - high-throughput version.

Runs directly in PyCharm (press Run) or from a terminal.

## What changed versus the previous version

The previous run reported:

    cache=60.7%   speed=108.8 papers/s   hit=2.19M

Profiling the checkpoint (5,000 records) explains the numbers:

    prompt tokens    mean 1,248   (system prompt ~1,107 + user ~141)
    cache hit        mean   747   -> 59.9%, matching the reported 60.7%
    completion       mean    25
    failures         0, all succeeded on attempt 1

So the API was not failing - it was **billed full price for 40% of a prompt
that is 89% constant**. Five fixes, in order of impact:

1. CACHE: move the volatile user text OUT of the cached prefix.
   The system prompt is byte-identical for every request and is now passed as
   a plain (non-f) string built once at import. Nothing about a paper appears
   before it. Expected hit rate: ~89% instead of 60%.

2. SPEED: the throughput cap was concurrency, not the model.
   108 papers/s at 150 concurrent and ~0.18 s latency is nowhere near the
   limit; the real constraint is that the old code awaited a whole
   SCHEDULE_BATCH_SIZE wave before writing the checkpoint. Requests are now
   dispatched by a worker pool that keeps N in flight continuously, so
   throughput is no longer gated by the slowest request in a wave.

3. ROBUSTNESS: `max_tokens=100` truncates reasoning models.
   The completion mean is 25 tokens, but a JSON object plus a stray sentence
   exceeds 100 and the response arrives cut off, failing the JSON parse. Raised
   to 512; a truncated response is now a *retryable* error rather than a
   permanent failure.

4. CORRECTNESS: the JSON parse was brittle.
   `json.loads` on the raw content fails whenever the model wraps the object in
   prose or a code fence. Now the first balanced {...} block is extracted, and
   out-of-range codes are repaired (T6 -> T06, 6 -> T06) instead of discarded.

5. VISIBILITY: the error text was swallowed.
   A failed record kept only `str(error)`, so the failure mode was invisible in
   the checkpoint. Errors are now recorded with their type and are counted per
   category in the final report.

## Configuration

All knobs sit at the top of the file. The defaults are tuned for the observed
latency; raise MAX_CONCURRENCY until errors appear, then back off ~20%.

## Output

    database/analysis_literature_input.jsonl              per-paper prompts
    database/deepseek_classification_checkpoint.jsonl     one line per paper
    database/analysis_literature_deepseek.parquet         input + result columns

Re-running is safe: completed row_index values are skipped, so an interrupted
run resumes where it stopped. Pass --retry-failed to re-attempt failures.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Any

import pandas as pd
from openai import AsyncOpenAI
from tqdm import tqdm

# ============================================================
# Paths
# ============================================================

ROOT = Path.cwd()
DB = ROOT / "database"

INPUT_PARQUET = DB / "analysis_literature.parquet"
INPUT_JSONL = DB / "analysis_literature_input.jsonl"
CHECKPOINT_JSONL = DB / "deepseek_classification_checkpoint.jsonl"
OUTPUT_PARQUET = DB / "analysis_literature_deepseek.parquet"

sys.stdout.reconfigure(encoding="utf-8")


# ============================================================
# API configuration
# ============================================================

DEEPSEEK_API_KEY = os.environ.get(
    "DEEPSEEK_API_KEY", "").strip()
API_BASE_URL = "https://api.deepseek.com"

# `deepseek-flash` is not a public model id; the chat models are
# deepseek-chat (V3) and deepseek-reasoner (R1). Override with --model.
MODEL = "deepseek-chat"

REQUEST_TIMEOUT = 180
MAX_RETRIES = 6

# Concurrency. The previous run used 150 and was nowhere near the API ceiling;
# throughput was gated by wave scheduling, not by this number. Raise it in
# steps of 50 and watch the error rate.
MAX_CONCURRENCY = 256

# How many results may sit unflushed before the checkpoint is written. Smaller
# means less work lost on a crash; 2000 keeps fsync overhead negligible.
CHECKPOINT_FLUSH_EVERY = 2000

# Completion length. The mean is ~25 tokens, but a JSON object plus a sentence
# of preamble overruns 100 and the response arrives truncated.
MAX_OUTPUT_TOKENS = 512

# How much of the abstract to send. Papers average ~1.2k characters; the cap
# keeps pathological records (full-text dumps) from inflating the miss cost.
MAX_ABSTRACT_CHARS = 6000

REBUILD_INPUT_JSONL = False   # set True to regenerate the prompt file
DEEPSEEK_USER_ID = "abm-literature-analysis"

# Records with no abstract are not classified. Stage 3 keeps an abstractless
# record when its title carries an ABM anchor - it is real evidence that the
# paper is agent-based - but the classifier only sees title + abstract, and
# title alone produces low-confidence guesses that would cost a full API call
# each. Such rows are left unclassified and flagged with
# `llm_skip_reason = "no_abstract"` so the topic analysis can exclude them,
# while the bibliometric and author-level work still counts them.
SKIP_NO_ABSTRACT = True
OUT_SKIPPED = DB / "deepseek_skipped_no_abstract.csv"


# ============================================================
# Taxonomies
# ============================================================

TOPIC_TAXONOMY = """
T01 Social behavior and social interaction
T02 Social networks and communication
T03 Culture, norms, identity, and values
T04 Population and migration
T05 Urban systems and urban evolution
T06 Transportation and mobility
T07 Land use and spatial planning
T08 Housing and real estate
T09 Economic systems and market behavior
T10 Finance and financial markets
T11 Organizations and firm behavior
T12 Labor markets and employment
T13 Public policy and governance
T14 Politics and collective action
T15 Health behavior and public health
T16 Infectious disease and epidemic diffusion
T17 Healthcare systems and medical services
T18 Ecology and ecosystems
T19 Environmental change and pollution
T20 Climate change and climate adaptation
T21 Energy systems
T22 Agriculture and land resources
T23 Water and natural resource management
T24 Disaster and emergency management
T25 Conflict, war, and security
T26 Crime and public safety
T27 Education and learning
T28 Tourism and leisure
T29 Science, technology, and innovation diffusion
T30 Robotics and autonomous systems
T31 Artificial intelligence and computational intelligence
T32 Methodology and computational modeling
"""

AGENT_TAXONOMY = """
A01 Individual humans
A02 Households and communities
A03 Social groups and collective actors
A04 Consumers
A05 Firms and organizations
A06 Governments and policy actors
A07 Patients, physicians, and healthcare actors
A08 Students and teachers
A09 Farmers and resource users
A10 Biological and ecological entities
A11 Vehicles and transportation agents
A12 Robots and autonomous systems
A13 LLM-based agents
A14 Virtual and software agents
A15 Hybrid multi-agent systems
"""

LLM_ROLE_TAXONOMY = """
L01 No LLM used
L02 Agent behavior generation
L03 Agent decision-making
L04 Agent reasoning and cognition
L05 Agent personality and memory
L06 Natural language interaction
L07 Social interaction simulation
L08 Synthetic population or agent generation
L09 Scenario generation
L10 Environment or world generation
L11 Text generation
L12 Data augmentation
L13 Information extraction and NLP analysis
L14 Prediction and forecasting
L15 Model or program generation
L16 Parameter estimation and model calibration
L17 Experiment design and experimental assistance
L18 Multiple or integrated LLM roles
L19 Other LLM role
L20 Unclear LLM role
"""


# ============================================================
# System prompt
#
# This string is byte-identical for every request, which is what makes it
# cacheable. It is built ONCE, at import, with no per-paper substitution -
# the paper text goes in the user message only. Any value interpolated here
# would sit before the volatile part and break prefix caching for every
# request that follows it.
# ============================================================

SYSTEM_PROMPT = (
    "You are an expert researcher in agent-based modeling (ABM), complex "
    "systems, computational social science, artificial intelligence, and "
    "large language model research.\n\n"
    "You will classify scientific papers using ONLY the title and abstract.\n\n"
    "Return a JSON object with exactly four fields:\n"
    '1. "topic": primary research topic\n'
    '2. "agent": dominant simulated agent category\n'
    '3. "llm_used": whether a large language model is actually used (0 or 1)\n'
    '4. "llm_role": the primary role of the LLM\n\n'
    "TOPIC TAXONOMY:\n" + TOPIC_TAXONOMY.strip() + "\n\n"
    "AGENT TAXONOMY:\n" + AGENT_TAXONOMY.strip() + "\n\n"
    "LLM ROLE TAXONOMY:\n" + LLM_ROLE_TAXONOMY.strip() + "\n\n"
    "CLASSIFICATION RULES:\n"
    "1. Select exactly ONE primary topic.\n"
    "2. Select exactly ONE dominant agent category.\n"
    "3. Use only information supported by the title and abstract.\n"
    "4. Do not infer unsupported information.\n"
    "5. An LLM counts as used only when it actually participates in the "
    "research.\n"
    "6. Merely mentioning GPT, ChatGPT, LLM, generative AI, or language models "
    "in background, related work, discussion, or future work does NOT count.\n"
    "7. Using an LLM only for manuscript writing, proofreading, translation, "
    "editing, or literature searching does NOT count as research-method LLM "
    "usage.\n"
    "8. If an LLM is used inside an agent architecture, simulation, decision "
    "process, behavior generation, reasoning process, synthetic-agent "
    "generation, prediction system, data generation, or another research "
    "methodology, classify llm_used as 1.\n"
    "9. Traditional machine learning, reinforcement learning, neural networks, "
    "transformers, or conventional NLP models are NOT automatically LLMs.\n"
    "10. GPT, ChatGPT, Claude, Gemini, Llama, Mistral, DeepSeek and similar "
    "large language models count as LLMs when they are actually used.\n"
    "11. Select A13 only when an LLM is actually part of the agent "
    "architecture.\n"
    "12. If llm_used is 0, llm_role MUST be L01.\n"
    "13. If an LLM is clearly used but its exact role cannot be determined, "
    "use L20.\n"
    "14. If multiple LLM roles are used and no dominant role can be "
    "identified, use L18.\n"
    "15. Do not output explanations.\n"
    "16. Do not output markdown.\n"
    "17. Return valid JSON only.\n"
    '18. Return exactly: {"topic":"T06","agent":"A11","llm_used":1,'
    '"llm_role":"L03"}\n'
)


# ============================================================
# Text handling
# ============================================================

def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    text = str(value).replace("\x00", " ")
    return re.sub(r"\s+", " ", text).strip()


# ============================================================
# Step 1 - parquet to jsonl
# ============================================================

def convert_parquet_to_jsonl() -> int:
    print("=" * 78)
    print("STEP 1: converting parquet to jsonl")
    print("=" * 78)

    if not INPUT_PARQUET.exists():
        raise FileNotFoundError(f"input parquet not found:\n{INPUT_PARQUET}")

    df = pd.read_parquet(INPUT_PARQUET)
    print(f"input: {INPUT_PARQUET}")
    print(f"rows:  {len(df):,}")

    for column in ("title", "abstract"):
        if column not in df.columns:
            raise ValueError(f"required column '{column}' not found")

    INPUT_JSONL.parent.mkdir(parents=True, exist_ok=True)

    skipped_rows = []
    with open(INPUT_JSONL, "w", encoding="utf-8") as out:
        for index, row in tqdm(df.iterrows(), total=len(df),
                               desc="converting", unit="paper"):
            abstract = normalize_text(row.get("abstract", ""))
            if not abstract and SKIP_NO_ABSTRACT:
                skipped_rows.append(index)
                continue
            if len(abstract) > MAX_ABSTRACT_CHARS:
                abstract = abstract[:MAX_ABSTRACT_CHARS] + " [...]"
            record = {
                "row_index": int(index),
                # `doi` is not sent to the model; it is the fingerprint that lets
                # load_checkpoint() detect that the corpus was rebuilt. Without
                # it a stale checkpoint keyed on row_index would attach one
                # paper's labels to another paper.
                "doi": normalize_text(row.get("doi", "")),
                "title": normalize_text(row.get("title", "")),
                "abstract": abstract,
            }
            out.write(json.dumps(record, ensure_ascii=False,
                                 separators=(",", ":")) + "\n")

    if skipped_rows:
        cols = [c for c in ("title", "authors", "year", "journal", "doi",
                            "source", "times_cited") if c in df.columns]
        df.loc[skipped_rows, cols].to_csv(OUT_SKIPPED, index=False,
                                          encoding="utf-8-sig")
        print(f"skipped {len(skipped_rows):,} rows with no abstract "
              f"-> {OUT_SKIPPED.name}")

    print(f"jsonl created: {INPUT_JSONL}")
    return len(df)


def load_input_records() -> list[dict]:
    records = []
    with open(INPUT_JSONL, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


# ============================================================
# Checkpoint
#
# The checkpoint is append-only jsonl: every attempt writes one line, and the
# last successful line per row_index wins. Reading it tolerates partial final
# lines (a killed process can leave one) and older records that lack the
# token-usage fields.
# ============================================================

def load_checkpoint(retry_failed: bool = False,
                    records: list[dict] | None = None) -> dict[int, dict]:
    """Load successful results, discarding any that belong to a different corpus.

    `records` is the current input; when supplied, each checkpoint entry's `doi`
    is compared against the DOI now sitting at that row_index. A mismatch means
    the corpus was rebuilt (re-collected, re-filtered) and row_index no longer
    identifies the same paper, so the whole checkpoint is discarded. Without
    this check the labels of one paper would be silently written onto another.
    """
    completed: dict[int, dict] = {}
    if not CHECKPOINT_JSONL.exists():
        return completed

    bad_lines = 0
    with open(CHECKPOINT_JSONL, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                bad_lines += 1          # partial trailing line from a hard kill
                continue

            row_index = record.get("row_index")
            if row_index is None:
                continue
            row_index = int(row_index)

            if record.get("status") == "success":
                completed[row_index] = record
            elif retry_failed:
                completed.pop(row_index, None)

    if bad_lines:
        print(f"  [warn] skipped {bad_lines} malformed checkpoint line(s)")

    if records and completed:
        # Compare on a sample: if the DOIs disagree the whole checkpoint is stale.
        # Look the row up BY ITS row_index, not by list position: records with no
        # abstract are not written to the jsonl, so the list is no longer dense
        # and position i no longer identifies row i.
        by_index = {int(r["row_index"]): r for r in records
                    if r.get("row_index") is not None}
        checked = mismatched = 0
        for row_index, rec in completed.items():
            current = by_index.get(row_index)
            if current is None:
                mismatched += 1
                continue
            old = str(rec.get("doi") or "")
            new = str(current.get("doi") or "")
            if not old and not new:
                continue
            checked += 1
            if old != new:
                mismatched += 1
        if checked and mismatched / checked > 0.05:
            print(f"  [!] checkpoint does not match the current corpus "
                  f"({mismatched}/{checked} DOIs differ) - the corpus was "
                  f"rebuilt, so the old labels are discarded.")
            return {}

    return completed


# ============================================================
# Model output parsing
# ============================================================

VALID_TOPICS = {f"T{i:02d}" for i in range(1, 33)}
VALID_AGENTS = {f"A{i:02d}" for i in range(1, 16)}
VALID_ROLES = {f"L{i:02d}" for i in range(1, 21)}

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)


def extract_json_object(content: str) -> dict:
    """Pull the first balanced {...} block out of the model output.

    A reasoning model may wrap the object in prose or a code fence, and
    `json.loads` on the raw text then fails even though the answer is present.
    Scanning for balanced braces also survives a trailing sentence.
    """
    if not content or not content.strip():
        raise ValueError("empty model output")

    text = content.strip()

    fence = _FENCE_RE.search(text)
    if fence:
        text = fence.group(1).strip()

    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    if start < 0:
        raise ValueError(f"no JSON object in output: {text[:120]!r}")

    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start:i + 1]
                data = json.loads(candidate)
                if not isinstance(data, dict):
                    raise ValueError("extracted JSON is not an object")
                return data

    # Unbalanced braces: the response was almost certainly truncated by
    # max_tokens, which the caller treats as retryable.
    raise ValueError(f"unbalanced JSON (likely truncated): {text[:120]!r}")


def coerce_code(value: Any, prefix: str, valid: set[str]) -> str:
    """Repair a code that is recognisable but not in canonical form.

    Handles 'T6' -> 'T06', '6' -> 'T06', 't06' -> 'T06', and a code that
    arrives as a number. Anything unrecognisable raises, so the caller retries
    rather than silently recording a wrong label.
    """
    s = str(value).strip().upper()
    if s in valid:
        return s
    m = re.fullmatch(rf"{prefix}?0*(\d+)", s)
    if m:
        code = f"{prefix}{int(m.group(1)):02d}"
        if code in valid:
            return code
    raise ValueError(f"unrecognised {prefix} code: {value!r}")


def parse_classification(content: str) -> dict:
    data = extract_json_object(content)

    missing = [f for f in ("topic", "agent", "llm_used", "llm_role")
               if f not in data]
    if missing:
        raise ValueError(f"missing field(s): {missing}")

    llm_used = data["llm_used"]
    if isinstance(llm_used, bool):
        llm_used = int(llm_used)
    elif isinstance(llm_used, str):
        s = llm_used.strip().lower()
        llm_used = 1 if s in ("1", "true", "yes") else 0
    llm_used = int(llm_used)
    if llm_used not in (0, 1):
        raise ValueError(f"invalid llm_used: {llm_used!r}")

    topic = coerce_code(data["topic"], "T", VALID_TOPICS)
    agent = coerce_code(data["agent"], "A", VALID_AGENTS)
    llm_role = coerce_code(data["llm_role"], "L", VALID_ROLES)

    if llm_used == 0:
        llm_role = "L01"

    return {"topic": topic, "agent": agent,
            "llm_used": llm_used, "llm_role": llm_role}


# ============================================================
# Client
# ============================================================

def create_client() -> AsyncOpenAI:
    if not DEEPSEEK_API_KEY or DEEPSEEK_API_KEY == "YOUR_DEEPSEEK_API_KEY":
        raise RuntimeError("set DEEPSEEK_API_KEY (env var or in this file)")
    return AsyncOpenAI(api_key=DEEPSEEK_API_KEY, base_url=API_BASE_URL,
                       timeout=REQUEST_TIMEOUT, max_retries=0)


def build_user_prompt(record: dict) -> str:
    return (
        "Classify the following scientific paper.\n\n"
        f"TITLE:\n{record.get('title', '')}\n\n"
        f"ABSTRACT:\n{record.get('abstract', '')}\n\n"
        "Return JSON only."
    )


# ============================================================
# One request
# ============================================================

RETRYABLE_MARKERS = ("429", "rate limit", "timeout", "timed out",
                     "connection", "502", "503", "504", "overload",
                     "truncat", "unbalanced", "empty model output")


def _is_retryable(error: Exception) -> bool:
    text = f"{type(error).__name__}: {error}".lower()
    return any(marker in text for marker in RETRYABLE_MARKERS)


async def classify_one(client: AsyncOpenAI, record: dict,
                       semaphore: asyncio.Semaphore) -> dict:
    row_index = int(record["row_index"])
    last_error = ""
    last_type = ""

    async with semaphore:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = await client.chat.completions.create(
                    model=MODEL,
                    messages=[
                        # fixed prefix first - this is the cached span
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": build_user_prompt(record)},
                    ],
                    response_format={"type": "json_object"},
                    max_tokens=MAX_OUTPUT_TOKENS,
                    temperature=0,
                    stream=False,
                    user=DEEPSEEK_USER_ID,
                )

                result = parse_classification(
                    response.choices[0].message.content)

                usage = response.usage
                get = (lambda k: (getattr(usage, k, 0) or 0)) if usage else (lambda k: 0)

                return {
                    "status": "success",
                    "row_index": row_index,
                    "doi": record.get("doi", ""),
                    **result,
                    "prompt_tokens": get("prompt_tokens"),
                    "completion_tokens": get("completion_tokens"),
                    "total_tokens": get("total_tokens"),
                    "cache_hit_tokens": get("prompt_cache_hit_tokens"),
                    "cache_miss_tokens": get("prompt_cache_miss_tokens"),
                    "attempt": attempt,
                    "timestamp": time.time(),
                }

            except Exception as error:
                last_error = str(error)
                last_type = type(error).__name__

                if attempt < MAX_RETRIES:
                    if _is_retryable(error):
                        delay = min(30.0, 1.5 ** attempt) + random.uniform(0, 1.5)
                    else:
                        delay = min(10.0, 0.5 * attempt)
                    await asyncio.sleep(delay)

    return {
        "status": "failed",
        "row_index": row_index,
        "error": last_error,
        "error_type": last_type,
        "attempts": MAX_RETRIES,
        "timestamp": time.time(),
    }


# ============================================================
# Driver
#
# Requests are kept in flight continuously by a pool of worker coroutines that
# pull from a shared iterator, instead of awaiting one fixed-size wave at a
# time. Wave scheduling idles every slot until the slowest request in the wave
# returns, which is what capped the previous run.
# ============================================================

async def run_analysis(records: list[dict], retry_failed: bool = False) -> None:
    completed = load_checkpoint(retry_failed=retry_failed, records=records)

    pending = [r for r in records if int(r["row_index"]) not in completed]

    print("=" * 78)
    print("STEP 2: DeepSeek classification")
    print("=" * 78)
    print(f"total papers:      {len(records):,}")
    print(f"already completed: {len(completed):,}")
    print(f"remaining:         {len(pending):,}")
    print(f"model:             {MODEL}")
    print(f"concurrency:       {MAX_CONCURRENCY}")
    print(f"max output tokens: {MAX_OUTPUT_TOKENS}")

    if not pending:
        print("nothing to do.")
        return

    CHECKPOINT_JSONL.parent.mkdir(parents=True, exist_ok=True)
    client = create_client()

    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    start_time = time.time()

    stats = {
        "success": len(completed), "failed": 0,
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        "cache_hit": 0, "cache_miss": 0,
    }
    errors: dict[str, int] = {}

    progress = tqdm(total=len(records), initial=len(completed),
                    desc="classifying", unit="paper")

    pending_iter = iter(pending)
    write_lock = asyncio.Lock()
    pending_writes: list[dict] = []
    stopped = False

    with open(CHECKPOINT_JSONL, "a", encoding="utf-8") as checkpoint:
        def flush() -> None:
            if not pending_writes:
                return
            for result in pending_writes:
                checkpoint.write(json.dumps(
                    result, ensure_ascii=False, separators=(",", ":")) + "\n")
            pending_writes.clear()
            checkpoint.flush()
            os.fsync(checkpoint.fileno())

        async def worker() -> None:
            nonlocal stopped
            while not stopped:
                try:
                    record = next(pending_iter)
                except StopIteration:
                    return

                result = await classify_one(client, record, semaphore)

                async with write_lock:
                    pending_writes.append(result)
                    if result["status"] == "success":
                        stats["success"] += 1
                        for key, field in (
                            ("prompt_tokens", "prompt_tokens"),
                            ("completion_tokens", "completion_tokens"),
                            ("total_tokens", "total_tokens"),
                            ("cache_hit", "cache_hit_tokens"),
                            ("cache_miss", "cache_miss_tokens"),
                        ):
                            stats[key] += result.get(field, 0) or 0
                    else:
                        stats["failed"] += 1
                        key = result.get("error_type") or "unknown"
                        errors[key] = errors.get(key, 0) + 1

                    if len(pending_writes) >= CHECKPOINT_FLUSH_EVERY:
                        flush()

                    progress.update(1)

                    cache_total = stats["cache_hit"] + stats["cache_miss"]
                    cache_rate = (100 * stats["cache_hit"] / cache_total
                                  if cache_total else 0.0)
                    elapsed = max(time.time() - start_time, 1e-3)
                    progress.set_postfix(
                        rate=f"{stats['success'] / elapsed:.0f}/s",
                        cache=f"{cache_rate:.1f}%",
                        fail=stats["failed"],
                    )

        try:
            await asyncio.gather(*(worker() for _ in range(MAX_CONCURRENCY)))
        except KeyboardInterrupt:
            stopped = True
            print("\n  interrupted - flushing checkpoint before exit ...")
        finally:
            progress.close()
            flush()

    await client.close()

    elapsed = max(time.time() - start_time, 1e-3)
    processed = stats["success"] - len(completed) + stats["failed"]
    cache_total = stats["cache_hit"] + stats["cache_miss"]
    cache_rate = 100 * stats["cache_hit"] / cache_total if cache_total else 0.0

    print()
    print("=" * 78)
    print("classification finished")
    print("=" * 78)
    print(f"success (total):     {stats['success']:,}")
    print(f"failed:              {stats['failed']:,}")
    print(f"processed this run:  {processed:,}")
    print(f"prompt tokens:       {stats['prompt_tokens']:,}")
    print(f"completion tokens:   {stats['completion_tokens']:,}")
    print(f"cache hit tokens:    {stats['cache_hit']:,}")
    print(f"cache miss tokens:   {stats['cache_miss']:,}")
    print(f"cache hit rate:      {cache_rate:.2f}%")
    print(f"elapsed:             {elapsed / 60:.1f} min")
    print(f"average speed:       {processed / elapsed:.1f} papers/s")

    if errors:
        print("\nerrors by type:")
        for key, count in sorted(errors.items(), key=lambda x: -x[1]):
            print(f"  {count:>6,}  {key}")


# ============================================================
# Taxonomy labels
# ============================================================

def parse_taxonomy(taxonomy: str) -> dict[str, str]:
    mapping = {}
    for line in taxonomy.strip().splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) == 2:
            mapping[parts[0]] = parts[1]
    return mapping


TOPIC_LABELS = parse_taxonomy(TOPIC_TAXONOMY)
AGENT_LABELS = parse_taxonomy(AGENT_TAXONOMY)
LLM_ROLE_LABELS = parse_taxonomy(LLM_ROLE_TAXONOMY)


# ============================================================
# Step 3 - merge results back into the parquet
# ============================================================

def merge_results_to_parquet() -> None:
    print()
    print("=" * 78)
    print("STEP 3: merging results into parquet")
    print("=" * 78)

    df = pd.read_parquet(INPUT_PARQUET)
    records = load_input_records()
    completed = load_checkpoint(records=records)

    print(f"original rows:     {len(df):,}")
    print(f"classified:        {len(completed):,}")

    def column_of(field: str, labels: dict[str, str] | None = None) -> list:
        out = []
        for index in range(len(df)):
            result = completed.get(index)
            if result is None:
                out.append(None)
                continue
            value = result.get(field)
            out.append(labels.get(value) if labels else value)
        return out

    df["abm_topic"] = column_of("topic")
    df["abm_topic_label"] = column_of("topic", TOPIC_LABELS)
    df["agent_category"] = column_of("agent")
    df["agent_category_label"] = column_of("agent", AGENT_LABELS)
    df["llm_used"] = column_of("llm_used")
    df["llm_role"] = column_of("llm_role")
    df["llm_role_label"] = column_of("llm_role", LLM_ROLE_LABELS)

    # Mark why a row has no label, so a topic breakdown can exclude it without
    # having to re-derive the reason. "no_abstract" = deliberately not sent.
    skip_reason = []
    for index in range(len(df)):
        abstract = normalize_text(df.at[index, "abstract"]) if "abstract" in df.columns else ""
        if not abstract and SKIP_NO_ABSTRACT and index not in completed:
            skip_reason.append("no_abstract")
        elif index not in completed:
            skip_reason.append("not_classified")
        else:
            skip_reason.append("")
    df["llm_skip_reason"] = skip_reason

    OUTPUT_PARQUET.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(OUTPUT_PARQUET, engine="pyarrow", index=False)

    print(f"\noutput: {OUTPUT_PARQUET}")
    print(f"rows:   {len(df):,}")
    print("added columns: abm_topic, abm_topic_label, agent_category, "
          "agent_category_label, llm_used, llm_role, llm_role_label")

    classified = df["abm_topic"].notna().sum()
    if classified:
        llm = pd.to_numeric(df["llm_used"], errors="coerce").fillna(0).eq(1).sum()
        print(f"\nclassified: {classified:,} / {len(df):,} "
              f"({100 * classified / len(df):.1f}%)")
        print(f"llm_used=1: {int(llm):,} ({100 * llm / classified:.2f}% of classified)")


# ============================================================
# Entry point
# ============================================================

async def main() -> None:
    # `global` must precede any reference to these names in the function body,
    # so the argument parser defaults read the module constants directly.
    global MODEL, MAX_CONCURRENCY

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="deepseek-chat")
    ap.add_argument("--concurrency", type=int, default=256)
    ap.add_argument("--rebuild-input", action="store_true",
                    help="regenerate the prompt jsonl from the parquet")
    ap.add_argument("--retry-failed", action="store_true",
                    help="re-attempt rows whose checkpoint line is a failure")
    ap.add_argument("--skip-merge", action="store_true",
                    help="classify only; do not write the output parquet")
    args = ap.parse_args()

    MODEL = args.model
    MAX_CONCURRENCY = args.concurrency

    print()
    print("=" * 78)
    print("ABM literature classification - DeepSeek")
    print("=" * 78)

    if args.rebuild_input or REBUILD_INPUT_JSONL or not INPUT_JSONL.exists():
        convert_parquet_to_jsonl()
    else:
        print(f"using existing jsonl: {INPUT_JSONL}")

    records = load_input_records()
    print(f"\nloaded {len(records):,} papers")

    await run_analysis(records, retry_failed=args.retry_failed)

    if not args.skip_merge:
        merge_results_to_parquet()

    print()
    print("=" * 78)
    print("done")
    print("=" * 78)


if __name__ == "__main__":
    asyncio.run(main())
