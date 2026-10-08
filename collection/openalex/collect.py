"""Collect English urban and rural planning works from OpenAlex Topics."""

from __future__ import annotations

import csv
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Iterable

import requests


HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
BASE_URL = "https://api.openalex.org"
API_KEY = os.getenv("OPENALEX_API_KEY", "").strip()
HARD_CREDIT_CAP = 10_000
MAX_CREDITS = min(int(os.getenv("OPENALEX_MAX_CREDITS", HARD_CREDIT_CAP)), HARD_CREDIT_CAP)
PER_PAGE = 100
TOPIC_BATCH_SIZE = 100
EXCLUDED_TYPES = {
    "review", "editorial", "letter", "book-review", "correction",
    "retraction", "news", "peer-review", "other",
}
RETRY_LIMIT = 5

TOPICS_FILE = DATA / "openalex_topics.json"
SELECTED_FILE = DATA / "selected_topics.csv"
WORKS_FILE = DATA / "works.jsonl"
STATE_FILE = DATA / "run_state.json"
LOG_FILE = DATA / "run.log"
CREDITS_FILE = DATA / "credit_usage.json"


def setup_logging() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )


def read_json(path: Path, default: Any) -> Any:
    if path.exists():
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    return default


def write_json(path: Path, value: Any) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
    temp.replace(path)


class OpenAlexClient:
    def __init__(self, credits_used: int = 0) -> None:
        if not API_KEY:
            raise SystemExit("请先设置环境变量 OPENALEX_API_KEY。")
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "LiteratureResearchAgent/1.0 (OpenAlex collection)",
            "Authorization": f"Bearer {API_KEY}",
        })
        self.credits_used = credits_used

    def get(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        query = dict(params or {})
        for attempt in range(RETRY_LIMIT):
            try:
                # Filter/list calls currently cost one credit each. Never issue
                # another request once the local run cap has been consumed.
                if self.credits_used >= MAX_CREDITS:
                    raise CreditLimitReached(f"已达到本次运行上限 {MAX_CREDITS} credits。")
                response = self.session.get(f"{BASE_URL}/{endpoint.lstrip('/')}", params=query, timeout=60)
                if response.status_code == 429 or response.status_code >= 500:
                    if response.status_code == 429 and "Insufficient budget" in response.text:
                        raise RuntimeError(
                            f"OpenAlex credits unavailable: {response.text[:500]}"
                        )
                    if attempt + 1 == RETRY_LIMIT:
                        raise RuntimeError(
                            f"OpenAlex HTTP {response.status_code}: {response.text[:500]}"
                        )
                    wait = min(60, 2 ** attempt)
                    logging.warning("HTTP %s; %s 秒后重试", response.status_code, wait)
                    time.sleep(wait)
                    continue
                if not response.ok:
                    raise RuntimeError(f"OpenAlex HTTP {response.status_code}: {response.text[:500]}")
                cost = int(response.headers.get("X-RateLimit-Credits-Used", "0"))
                if self.credits_used + cost > MAX_CREDITS:
                    raise CreditLimitReached(
                        f"本次请求将使使用量超过上限：已用 {self.credits_used}，请求成本 {cost}，上限 {MAX_CREDITS}。"
                    )
                self.credits_used += cost
                write_json(CREDITS_FILE, {"cumulative_credits_used": self.credits_used, "limit": MAX_CREDITS})
                logging.info("请求 %s | 本次运行积分 %s/%s", endpoint, self.credits_used, MAX_CREDITS)
                return response.json()
            except requests.RequestException:
                if attempt + 1 == RETRY_LIMIT:
                    raise
                time.sleep(min(60, 2 ** attempt))
        raise RuntimeError("OpenAlex request failed")


class CreditLimitReached(RuntimeError):
    pass


def normalize_id(value: str | None) -> str:
    if not value:
        return ""
    return value.rsplit("/", 1)[-1]


def hierarchy(topic: dict[str, Any]) -> dict[str, str]:
    subfield = topic.get("subfield") or {}
    field = topic.get("field") or {}
    domain = topic.get("domain") or {}
    return {
        "topic_id": normalize_id(topic.get("id")),
        "topic_name": topic.get("display_name", ""),
        "subfield_id": normalize_id(subfield.get("id")),
        "subfield_name": subfield.get("display_name", ""),
        "field_id": normalize_id(field.get("id")),
        "field_name": field.get("display_name", ""),
        "domain_id": normalize_id(domain.get("id")),
        "domain_name": domain.get("display_name", ""),
    }


def fetch_all_topics(client: OpenAlexClient) -> list[dict[str, Any]]:
    if TOPICS_FILE.exists():
        cached = read_json(TOPICS_FILE, [])
        if cached:
            logging.info("使用已保存的 Topics 分类，共 %d 项", len(cached))
            return cached
    cursor = "*"
    topics: list[dict[str, Any]] = []
    while cursor:
        page = client.get("topics", {"per_page": PER_PAGE, "cursor": cursor})
        topics.extend(page.get("results", []))
        cursor = (page.get("meta") or {}).get("next_cursor")
        logging.info("已读取 Topics %d/%s", len(topics), (page.get("meta") or {}).get("count", "?"))
    write_json(TOPICS_FILE, topics)
    return topics


def match_topics(topics: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rules = read_json(HERE / "topic_selection.json", {})
    keys = {
        "topic": [s.casefold() for s in rules.get("include_topic_name_phrases", [])],
        "subfield": [s.casefold() for s in rules.get("include_subfield_name_phrases", [])],
        "field": [s.casefold() for s in rules.get("include_field_name_phrases", [])],
    }
    selected = []
    for raw in topics:
        item = hierarchy(raw)
        checks = (
            ("topic", item["topic_name"]),
            ("subfield", item["subfield_name"]),
            ("field", item["field_name"]),
        )
        matches = [f"{level}:{phrase}" for level, name in checks for phrase in keys[level] if phrase in name.casefold()]
        if matches:
            item["matched_by"] = "; ".join(matches)
            selected.append(item)
    return selected


def save_selected_topics(selected: list[dict[str, Any]]) -> None:
    fields = list(hierarchy({}).keys()) + ["matched_by"]
    with SELECTED_FILE.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(selected)


def work_record(work: dict[str, Any]) -> dict[str, Any]:
    primary = work.get("primary_topic") or {}
    topics = work.get("topics") or []
    abstract_index = work.get("abstract_inverted_index") or {}
    abstract_words = [""] * (max((max(pos) for pos in abstract_index.values() if pos), default=-1) + 1)
    for word, positions in abstract_index.items():
        for pos in positions:
            abstract_words[pos] = word
    return {
        "openalex_id": work.get("id"),
        "doi": work.get("doi"),
        "title": work.get("title") or work.get("display_name"),
        "publication_year": work.get("publication_year"),
        "publication_date": work.get("publication_date"),
        "language": work.get("language"),
        "type": work.get("type"),
        "abstract": " ".join(abstract_words).strip(),
        "authorships": work.get("authorships", []),
        "primary_topic": hierarchy(primary) if primary else None,
        "topics": [hierarchy(t) for t in topics],
        "sources": work.get("locations", []),
        "cited_by_count": work.get("cited_by_count"),
        "openalex_created_date": work.get("created_date"),
        "openalex_updated_date": work.get("updated_date"),
    }


def selected_topic_ids_for_batch(selected: list[dict[str, Any]], start: int) -> list[str]:
    return [row["topic_id"] for row in selected[start : start + TOPIC_BATCH_SIZE] if row["topic_id"]]


def estimate_query(client: OpenAlexClient, ids: list[str]) -> tuple[int, int]:
    filt = "topics.id:" + "|".join(ids)
    data = client.get("works", {"filter": f"{filt},language:en", "per_page": 1, "select": "id"})
    count = int((data.get("meta") or {}).get("count", 0))
    pages = (count + PER_PAGE - 1) // PER_PAGE
    return count, pages


def main() -> None:
    setup_logging()
    if not 1 <= MAX_CREDITS <= HARD_CREDIT_CAP:
        raise SystemExit("OPENALEX_MAX_CREDITS 必须在 1 到 10000 之间。")
    usage = read_json(CREDITS_FILE, {"cumulative_credits_used": 0})
    prior_credits = int(usage.get("cumulative_credits_used", 0))
    if prior_credits >= MAX_CREDITS:
        raise SystemExit(f"项目已累计使用 {prior_credits} credits，达到上限 {MAX_CREDITS}。")
    client = OpenAlexClient(prior_credits)
    topics = fetch_all_topics(client)
    selected = match_topics(topics)
    save_selected_topics(selected)
    if not selected:
        raise SystemExit("没有匹配到主题；请检查 topic_selection.json。")
    logging.info("匹配到 %d 个候选主题，详见 %s", len(selected), SELECTED_FILE)

    state = read_json(STATE_FILE, {"next_topic_offset": 0, "cursor": "*", "processed_ids": 0, "kept_ids": 0})
    # A paper can carry several selected topics and therefore appear in more
    # than one 100-topic batch. Also load IDs already written for safe resume.
    seen_ids: set[str] = set()
    if WORKS_FILE.exists():
        with WORKS_FILE.open(encoding="utf-8") as existing:
            for line in existing:
                try:
                    record = json.loads(line)
                    if record.get("openalex_id"):
                        seen_ids.add(record["openalex_id"])
                except json.JSONDecodeError:
                    logging.warning("忽略 works.jsonl 中无法读取的一行")
    offset = int(state.get("next_topic_offset", 0))
    with WORKS_FILE.open("a", encoding="utf-8") as output:
        while offset < len(selected):
            batch_size = int(state.get("active_topic_batch_size", TOPIC_BATCH_SIZE))
            ids = selected_topic_ids_for_batch(selected, offset)[:batch_size]
            if not ids:
                offset += TOPIC_BATCH_SIZE
                continue
            try:
                count, pages = estimate_query(client, ids)
            except CreditLimitReached as exc:
                logging.warning("积分不足以继续估算主题批次：%s", exc)
                break
            # One credit per filtered list call is the current documented rate;
            # reserve headroom for cursor pages and cost variation.
            estimated_cost = pages + 2
            remaining = MAX_CREDITS - client.credits_used
            while estimated_cost > remaining and batch_size > 1:
                batch_size = max(1, batch_size // 2)
                ids = selected_topic_ids_for_batch(selected, offset)[:batch_size]
                try:
                    count, pages = estimate_query(client, ids)
                except CreditLimitReached as exc:
                    logging.warning("积分不足以继续估算主题批次：%s", exc)
                    break
                estimated_cost = pages + 2
                remaining = MAX_CREDITS - client.credits_used
            remaining = MAX_CREDITS - client.credits_used
            if estimated_cost > remaining:
                logging.warning(
                    "暂停：缩小主题批次后仍需约 %d credits（%d 篇，%d 页），本次剩余 %d。",
                    estimated_cost, count, pages, MAX_CREDITS - client.credits_used,
                )
                break
            state["active_topic_batch_size"] = batch_size
            state["next_topic_offset"] = offset
            write_json(STATE_FILE, state)
            logging.info("主题批次 %d-%d：约 %d 篇，%d 页", offset + 1, offset + len(ids), count, pages)
            cursor = state.get("cursor", "*")
            try:
                while cursor:
                    params = {
                        "filter": "topics.id:" + "|".join(ids) + ",language:en",
                        "per_page": PER_PAGE,
                        "cursor": cursor,
                    }
                    page = client.get("works", params)
                    records = page.get("results", [])
                    for work in records:
                        if work.get("type") in EXCLUDED_TYPES:
                            continue
                        work_id = work.get("id")
                        if work_id and work_id not in seen_ids:
                            output.write(json.dumps(work_record(work), ensure_ascii=False) + "\n")
                            seen_ids.add(work_id)
                            state["kept_ids"] = int(state.get("kept_ids", 0)) + 1
                    state["processed_ids"] = int(state.get("processed_ids", 0)) + len(records)
                    output.flush()
                    cursor = (page.get("meta") or {}).get("next_cursor")
                    state.update({
                        "next_topic_offset": offset,
                        "active_topic_batch_size": batch_size,
                        "cursor": cursor or "*",
                        "credits_used_this_run": client.credits_used,
                        "processed_ids": state["processed_ids"],
                        "kept_ids": state["kept_ids"],
                    })
                    write_json(STATE_FILE, state)
            except CreditLimitReached as exc:
                logging.warning("积分上限触发，已保存断点：%s", exc)
                return
            offset += len(ids)
            state.update({
                "next_topic_offset": offset,
                "active_topic_batch_size": TOPIC_BATCH_SIZE,
                "cursor": "*",
                "credits_used_this_run": client.credits_used - prior_credits,
            })
            write_json(STATE_FILE, state)
    logging.info("本次结束：使用 %d credits，累计筛选后记录 %d 篇", client.credits_used, state.get("kept_ids", 0))


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        logging.error("采集停止：%s", exc)
        raise SystemExit(1) from None
