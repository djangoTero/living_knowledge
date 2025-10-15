"""Fetches AI news and posts fresh items to Slack."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlencode

import requests

from common import (
    NewsItem,
    StateStore,
    compute_accuracy,
    compute_recency_score,
    configure_logging,
    domain_weight_for,
    load_domain_weights,
    make_item_id,
    normalize_url,
)

USER_AGENT = "ai-news-pipeline/1.0"
DEFAULT_LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "12"))
MAX_ITEMS = int(os.environ.get("MAX_ITEMS", "20"))
KEYWORDS = os.environ.get(
    "KEYWORDS", '"generative AI" OR "large language model" OR LLM OR "diffusion model" OR "AI safety" OR "machine learning model"'
)

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_CH_DAILY = os.environ.get("SLACK_CH_DAILY", "#ai-daily")
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4.1-mini")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

AI_DOMAINS = {
    "openai.com", "deepmind.com", "deepmind.google", "anthropic.com", "huggingface.co", "stability.ai",
    "cohere.ai", "nvidia.com", "research.google", "googleblog.com", "ai.facebook.com", "meta.com",
}

class SlackClient:
    """Lightweight Slack client that no-ops when credentials are missing."""

    def __init__(self, token: Optional[str]) -> None:
        self.token = token
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    def post_message(self, channel: str, text: str, blocks: Optional[List[Dict]] = None) -> Optional[str]:
        if not self.token:
            logging.info("[DRY RUN] Would post to %s: %s", channel, text[:120])
            return None
        url = "https://slack.com/api/chat.postMessage"
        payload = {"channel": channel, "text": text}
        if blocks:
            payload["blocks"] = json.dumps(blocks)
        headers = {"Authorization": f"Bearer {self.token}"}
        response = self.session.post(url, data=payload, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            error = data.get("error", "unknown_error")
            if error == "not_in_channel":
                logging.warning("Slack bot is not a member of %s; skipping post for '%s'", channel, text[:120])
                return None
            raise RuntimeError(f"Slack error: {data}")
        return data.get("ts")


def _looks_ai_related(title: str, source: str, url: str) -> bool:
    t = (title or "").lower()
    if any(k in t for k in ["ai", "llm", "large language model", "transformer", "diffusion", "rlhf", "genai"]):
        return True
    host = re.sub(r"^www\.", "", re.sub(r"^https?://", "", url)).split("/")[0]
    return host in AI_DOMAINS


class FeedFetcher:
    def __init__(self, lookback_hours: int) -> None:
        self.lookback_hours = lookback_hours
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

    def fetch(self) -> List[Dict[str, str]]:
        items: List[Dict[str, str]] = []
        items.extend(self._fetch_gdelt())
        items.extend(self._fetch_google_news())
        items.extend(self._fetch_hn())
        items.extend(self._fetch_curated_feeds())
        items.extend(self._fetch_substack_feeds())
        logging.info("Fetched %d raw items", len(items))
        return [i for i in items if _looks_ai_related(i.get("title", ""), i.get("source", ""), i.get("url", ""))]

    def _fetch_gdelt(self) -> List[Dict[str, str]]:
        params = {
            "query": f"({KEYWORDS})",
            "mode": "ArtList",
            "maxrecords": "50",
            "format": "JSON",
            "sort": "datedesc",
        }
        url = "https://api.gdeltproject.org/api/v2/doc/doc"
        response = self.session.get(url, params=params, timeout=30)
        response.raise_for_status()
        try:
            data = response.json()
        except ValueError:
            logging.warning("GDELT response was not JSON: %s", response.text[:200])
            return []
        results = []
        for entry in data.get("articles", []):
            published = entry.get("seendate") or entry.get("publishedDate")
            if not published:
                continue
            results.append({
                "url": entry.get("url"),
                "title": entry.get("title"),
                "source": entry.get("source", "GDELT"),
                "published": entry.get("seendate"),
            })
        return results

    def _fetch_google_news(self) -> List[Dict[str, str]]:
        query_params = {"q": KEYWORDS, "hl": "en-US", "gl": "US", "ceid": "US:en"}
        url = f"https://news.google.com/rss/search?{urlencode(query_params)}"
        return self._fetch_rss(url, source="Google News")

    def _fetch_hn(self) -> List[Dict[str, str]]:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=self.lookback_hours)
        params = {"tags": "story", "query": KEYWORDS, "numericFilters": f"created_at_i>{int(cutoff.timestamp())}"}
        url = "https://hn.algolia.com/api/v1/search"
        response = self.session.get(url, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
        results = []
        for hit in data.get("hits", []):
            if not hit.get("url"):
                continue
            results.append({
                "url": hit["url"],
                "title": hit.get("title") or hit.get("story_title"),
                "source": "Hacker News",
                "published": hit.get("created_at"),
            })
        return results

    def _fetch_curated_feeds(self) -> List[Dict[str, str]]:
        feeds = {
            "The Rundown AI": "https://www.therundown.ai/feed",
            "Ben's Bites": "https://www.bensbites.co/feed",
            "TLDR AI": "https://www.tldrnewsletter.com/ai/rss",
        }
        items: List[Dict[str, str]] = []
        for name, url in feeds.items():
            items.extend(self._fetch_rss(url, source=name))
        return items

    def _fetch_substack_feeds(self) -> List[Dict[str, str]]:
        feeds_env = os.environ.get("SUBSTACK_FEEDS")
        if not feeds_env:
            return []
        items: List[Dict[str, str]] = []
        for url in feeds_env.split(","):
            url = url.strip()
            if not url:
                continue
            items.extend(self._fetch_rss(url, source="Substack"))
        return items

    def _fetch_rss(self, url: str, source: str) -> List[Dict[str, str]]:
        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
        except requests.RequestException as exc:
            logging.warning("Failed to fetch RSS %s (%s): %s", source, url, exc)
            return []
        text = response.text
        return parse_rss(text, source)


def parse_rss(text: str, source: str) -> List[Dict[str, str]]:
    import xml.etree.ElementTree as ET

    items: List[Dict[str, str]] = []
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        logging.warning("Failed to parse RSS for %s: %s", source, exc)
        return items
    channel = root.find("channel")
    if channel is None:
        # Atom feed
        for entry in root.findall("{http://www.w3.org/2005/Atom}entry"):
            link_el = entry.find("{http://www.w3.org/2005/Atom}link")
            title_el = entry.find("{http://www.w3.org/2005/Atom}title")
            updated_el = entry.find("{http://www.w3.org/2005/Atom}updated")
            if link_el is None or title_el is None:
                continue
            href = link_el.attrib.get("href")
            if not href:
                continue
            items.append({
                "url": href,
                "title": (title_el.text or "").strip(),
                "source": source,
                "published": (updated_el.text if updated_el is not None else ""),
            })
        return items

    for item in channel.findall("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date = (item.findtext("pubDate") or item.findtext("updated") or "").strip()
        if not link:
            continue
        items.append({"url": link, "title": title, "source": source, "published": pub_date})
    return items


# -------- Batched enrichment --------

def enrich_batch(items: List[Dict[str, str]]) -> Dict[str, Tuple[str, str, str]]:
    """Return mapping id -> (meaning, impact, affected). Falls back to heuristics."""
    # Heuristic fast path if no key
    if not OPENAI_API_KEY:
        out: Dict[str, Tuple[str, str, str]] = {}
        for it in items:
            t = f"{it.get('title','')} {it.get('source','')}".lower()
            meaning, impact, affected = "AI update", "General", "Researchers"
            if "launch" in t:
                meaning, impact, affected = "Product launch", "Product", "Customers"
            elif "funding" in t:
                meaning, impact, affected = "Investment", "Finance", "Investors"
            out[it["id"]] = (meaning, impact, affected)
        return out

    try:
        from openai import OpenAI  # type: ignore
    except Exception:
        OPENAI_FALLBACK = True
    else:
        OPENAI_FALLBACK = False

    payload = [{"id": it["id"], "title": it.get("title", ""), "source": it.get("source", "")} for it in items]
    prompt = {
        "task": "Categorize AI news items. For each, return meaning, impact, affected.",
        "schema": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}, "meaning": {"type": "string"}, "impact": {"type": "string"}, "affected": {"type": "string"}}, "required": ["id", "meaning", "impact", "affected"]}}}, "required": ["items"]},
        "items": payload,
    }

    try:
        if not OPENAI_FALLBACK:
            client = OpenAI(api_key=OPENAI_API_KEY)
            resp = client.chat.completions.create(
                model=LLM_MODEL,
                response_format={"type": "json_object"},
                messages=[{"role": "user", "content": json.dumps(prompt)}],
                temperature=0,
            )
            content = resp.choices[0].message.content or "{}"
        else:
            import openai  # type: ignore
            openai.api_key = OPENAI_API_KEY
            resp = openai.ChatCompletion.create(
                model=LLM_MODEL,
                messages=[{"role": "user", "content": json.dumps(prompt)}],
                temperature=0,
            )
            content = resp["choices"][0]["message"]["content"]
        data = json.loads(content)
        results = {it["id"]: (it.get("meaning", "AI news"), it.get("impact", "General"), it.get("affected", "General")) for it in data.get("items", [])}
        return results
    except Exception as exc:  # Robust fallback
        logging.warning("OpenAI enrichment failed: %s", exc)
        out: Dict[str, Tuple[str, str, str]] = {}
        for it in items:
            out[it["id"]] = ("AI news", "General", "General")
        return out


def build_slack_blocks(item: NewsItem) -> List[Dict]:
    context = f"Accuracy: {item.accuracy} | Source: {item.source}"
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*<{item.url}|{item.title}>*"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": context}]},
    ]


def dedupe_items(raw_items: Iterable[Dict[str, str]]) -> Dict[str, Dict[str, str]]:
    deduped: Dict[str, Dict[str, str]] = {}
    groups: Dict[str, List[str]] = {}
    title_index: Dict[str, str] = {}
    for item in raw_items:
        url = item.get("url")
        if not url:
            continue
        normalized = normalize_url(url)
        item["normalized_url"] = normalized
        title_key = re.sub(r"\W+", "", (item.get("title") or "").lower())
        existing_key = None
        if normalized in deduped:
            existing_key = normalized
        elif title_key and title_key in title_index:
            existing_key = title_index[title_key]
        if existing_key is None:
            deduped[normalized] = item
            groups[normalized] = [item.get("source", "")]  # type: ignore[index]
            if title_key:
                title_index[title_key] = normalized
        else:
            groups.setdefault(existing_key, []).append(item.get("source", ""))
    for normalized, item in deduped.items():
        item["corroborations"] = len(groups[normalized]) - 1
    return deduped


def select_new_items(state: StateStore, candidates: List[NewsItem]) -> List[NewsItem]:
    fresh: List[NewsItem] = []
    for item in candidates:
        if state.get(item.id) is not None:
            continue
        fresh.append(item)
    fresh.sort(key=lambda x: x.accuracy, reverse=True)
    return fresh[:MAX_ITEMS]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Do not post to Slack")
    args = parser.parse_args()

    configure_logging()
    lookback_hours = DEFAULT_LOOKBACK_HOURS

    store = StateStore()
    store.load()

    fetcher = FeedFetcher(lookback_hours=lookback_hours)
    raw_items = fetcher.fetch()
    deduped = dedupe_items(raw_items)
    weights = load_domain_weights()

    now = dt.datetime.now(dt.timezone.utc)

    # Hazırlık: batch enrichment girişleri
    batch_input: List[Dict[str, str]] = []
    pre_candidates: List[Dict[str, object]] = []

    for normalized, item in deduped.items():
        published_raw = item.get("published") or now.isoformat()
        parsed = None
        for fmt in (
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%d %H:%M:%S",
            "%Y%m%d%H%M%S",
            "%a, %d %b %Y %H:%M:%S %Z",
            "%a, %d %b %Y %H:%M:%S %z",
        ):
            try:
                parsed = dt.datetime.strptime(published_raw.replace("Z", "+0000"), fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            try:
                parsed = dt.datetime.fromisoformat(published_raw.replace("Z", "+00:00"))
            except ValueError:
                # Tarihi yoksa en taze sayma: 24 saat eski kabul et
                parsed = now - dt.timedelta(hours=24)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        published_utc = parsed.astimezone(dt.timezone.utc)
        recency = compute_recency_score(published_utc, now=now, lookback_hours=lookback_hours)
        domain_weight = domain_weight_for(item["url"], weights)
        corroborations = item.get("corroborations", 0)
        accuracy = compute_accuracy(domain_weight, corroborations, recency)
        item_id = make_item_id(item["url"])
        pre_candidates.append({
            "id": item_id,
            "url": item["url"],
            "title": item.get("title") or "(untitled)",
            "source": item.get("source") or "unknown",
            "published_utc": published_utc.isoformat(),
            "accuracy": accuracy,
            "corroborations": corroborations,
        })
        batch_input.append({"id": item_id, "title": item.get("title") or "", "source": item.get("source") or ""})

    enrich_map = enrich_batch(batch_input)

    candidates: List[NewsItem] = []
    for raw in pre_candidates:
        meaning, impact, affected = enrich_map.get(raw["id"], ("AI news", "General", "General"))
        candidate = NewsItem(
            id=raw["id"],
            url=raw["url"],
            title=raw["title"],
            source=raw["source"],
            published_utc=raw["published_utc"],
            status="daily",
            accuracy=float(raw["accuracy"]),
            corroborations=int(raw["corroborations"]),
            meaning=meaning,
            impact=impact,
            affected=affected,
        )
        candidates.append(candidate)

    fresh_items = select_new_items(store, candidates)
    logging.info("Identified %d fresh items", len(fresh_items))

    slack = SlackClient(token=None if args.dry_run else SLACK_BOT_TOKEN)
    posted_count = 0

    for item in fresh_items:
        blocks = build_slack_blocks(item)
        ts = slack.post_message(SLACK_CH_DAILY, item.title, blocks=blocks)
        if ts:
            item.ts_daily = ts
        store.upsert(item)
        posted_count += 1

    if not fresh_items:
        logging.info("No new items to post")
    else:
        logging.info("Posted %d new items", posted_count)

    store.save()

    preview_path = os.environ.get("PREVIEW_JSON")
    if preview_path:
        with open(preview_path, "w", encoding="utf-8") as f:
            json.dump([item.to_json() for item in fresh_items], f, indent=2)


if __name__ == "__main__":
    main()
