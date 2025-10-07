#!/usr/bin/env python3
"""Promotes AI news items across Slack channels and manages markdown content."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import pathlib
from typing import Dict, Iterable, List, Optional

import requests

from common import NewsItem, StateStore, configure_logging, parse_datetime
from github_agent import GitHubAgent

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_CH_DAILY = os.environ.get("SLACK_CH_DAILY")
SLACK_CH_WEEKLY = os.environ.get("SLACK_CH_WEEKLY")
SLACK_CH_MONTHLY = os.environ.get("SLACK_CH_MONTHLY")

OVERVIEW_STATE_PATH = pathlib.Path(os.environ.get("OVERVIEW_STATE_PATH", "state/overviews.json"))

PROMOTE_WEEKLY_HOURS = 24
PROMOTE_MONTHLY_HOURS = 24 * 7
TTL_DAILY_HOURS = 48
TTL_WEEKLY_HOURS = 24 * 7
TTL_MONTHLY_HOURS = 24 * 90


class SlackMetricsClient:
    def __init__(self, token: Optional[str]) -> None:
        self.token = token
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "ai-news-pipeline/1.0"})
        if token:
            self.session.headers.update({"Authorization": f"Bearer {token}"})

    def _get(self, method: str, params: Dict) -> Dict:
        if not self.token:
            return {"ok": False}
        response = self.session.get(f"https://slack.com/api/{method}", params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    def _post(self, method: str, payload: Dict) -> Dict:
        if not self.token:
            logging.info("[DRY RUN] Would call %s with %s", method, payload)
            return {"ok": False}
        response = self.session.post(f"https://slack.com/api/{method}", data=payload, timeout=30)
        response.raise_for_status()
        return response.json()

    def fetch_metrics(self, channel: str, ts: str) -> Dict[str, int]:
        replies = 0
        pinned = False
        pushpin_reactions = 0
        if not self.token:
            return {"replies": replies, "pinned": pinned, "pushpins": pushpin_reactions}
        replies_resp = self._get("conversations.replies", {"channel": channel, "ts": ts})
        if replies_resp.get("ok"):
            replies = max(len(replies_resp.get("messages", [])) - 1, 0)
        pins_resp = self._get("pins.list", {"channel": channel})
        if pins_resp.get("ok"):
            for item in pins_resp.get("items", []):
                if item.get("message", {}).get("ts") == ts:
                    pinned = True
                    break
        reactions_resp = self._get("reactions.get", {"channel": channel, "timestamp": ts})
        if reactions_resp.get("ok"):
            message = reactions_resp.get("message", {})
            for reaction in message.get("reactions", []):
                if reaction.get("name") == "pushpin":
                    pushpin_reactions = reaction.get("count", 0)
        return {"replies": replies, "pinned": pinned, "pushpins": pushpin_reactions}

    def delete_message(self, channel: str, ts: str) -> None:
        if not self.token:
            logging.info("[DRY RUN] Would delete message in %s at %s", channel, ts)
            return
        resp = self._post("chat.delete", {"channel": channel, "ts": ts})
        if not resp.get("ok"):
            logging.warning("Failed to delete message: %s", resp)

    def post_overview(self, channel: str, text: str, ts: Optional[str]) -> Optional[str]:
        if not self.token:
            logging.info("[DRY RUN] Would update overview in %s", channel)
            return ts
        method = "chat.update" if ts else "chat.postMessage"
        payload = {"channel": channel, "text": text}
        if ts:
            payload["ts"] = ts
        resp = self._post(method, payload)
        if not resp.get("ok"):
            logging.warning("Failed to update overview: %s", resp)
            return ts
        new_ts = resp.get("ts")
        if method == "chat.postMessage":
            self._post("pins.add", {"channel": channel, "timestamp": new_ts})
        return new_ts


def should_promote_weekly(item: NewsItem, now: dt.datetime) -> bool:
    published = parse_datetime(item.published_utc)
    age_hours = (now - published).total_seconds() / 3600
    if age_hours < PROMOTE_WEEKLY_HOURS:
        return False
    if item.accuracy < 0.7:
        return False
    if item.corroborations >= 2 or item.replies >= 3 or item.pinned:
        return True
    return False


def should_promote_monthly(item: NewsItem, now: dt.datetime) -> bool:
    published = parse_datetime(item.published_utc)
    age_hours = (now - published).total_seconds() / 3600
    if age_hours < PROMOTE_MONTHLY_HOURS:
        return False
    if item.accuracy < 0.8:
        return False
    if item.corroborations >= 3 or item.replies >= 5:
        return True
    return False


def slack_ts_to_datetime(ts: str) -> dt.datetime:
    seconds = float(ts)
    return dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc)


def expired(item: NewsItem, now: dt.datetime) -> bool:
    if item.status == "daily" and item.ts_daily:
        ts = slack_ts_to_datetime(item.ts_daily)
        return (now - ts).total_seconds() / 3600 >= TTL_DAILY_HOURS
    if item.status == "weekly" and item.ts_weekly:
        ts = slack_ts_to_datetime(item.ts_weekly)
        return (now - ts).total_seconds() / 3600 >= TTL_WEEKLY_HOURS
    if item.status == "monthly" and item.ts_monthly:
        ts = slack_ts_to_datetime(item.ts_monthly)
        return (now - ts).total_seconds() / 3600 >= TTL_MONTHLY_HOURS
    return False


def update_overview(slack: SlackMetricsClient, channel: Optional[str], items: Iterable[NewsItem], ts: Optional[str]) -> Optional[str]:
    if not channel:
        return ts
    counts = len(list(items))
    text = f"AI news live overview: {counts} active stories"
    return slack.post_overview(channel, text, ts)


def load_overview_state() -> Dict[str, str]:
    if not OVERVIEW_STATE_PATH.exists():
        return {}
    return json.loads(OVERVIEW_STATE_PATH.read_text(encoding="utf-8"))


def save_overview_state(data: Dict[str, str]) -> None:
    OVERVIEW_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    OVERVIEW_STATE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    configure_logging()
    store = StateStore()
    store.load()
    slack = SlackMetricsClient(token=None if args.dry_run else SLACK_BOT_TOKEN)
    now = dt.datetime.now(dt.timezone.utc)

    # Update metrics
    channel_map = {"daily": SLACK_CH_DAILY, "weekly": SLACK_CH_WEEKLY, "monthly": SLACK_CH_MONTHLY}
    for item in store.values():
        ts = item.ts_daily if item.status == "daily" else item.ts_weekly if item.status == "weekly" else item.ts_monthly
        channel = channel_map.get(item.status)
        if not ts or not channel:
            continue
        metrics = slack.fetch_metrics(channel, ts)
        item.replies = metrics.get("replies", item.replies)
        item.pinned = metrics.get("pinned", item.pinned) or metrics.get("pushpins", 0) > 0

    promotions: List[NewsItem] = []
    removals: List[NewsItem] = []
    for item in list(store.values()):
        if item.status == "daily" and should_promote_weekly(item, now):
            item.status = "weekly"
            item.ts_weekly = item.ts_weekly or item.ts_daily
            promotions.append(item)
        if item.status == "weekly" and should_promote_monthly(item, now):
            item.status = "monthly"
            item.ts_monthly = item.ts_monthly or item.ts_weekly
            promotions.append(item)
        if expired(item, now):
            removals.append(item)

    for item in removals:
        channel = channel_map.get(item.status)
        ts = item.ts_daily if item.status == "daily" else item.ts_weekly if item.status == "weekly" else item.ts_monthly
        if channel and ts:
            slack.delete_message(channel, ts)
        store.remove(item.id)

    # Persist
    for item in promotions:
        store.upsert(item)
    store.save()

    # Sync markdown / PR
    agent = GitHubAgent(token=os.environ.get("GITHUB_TOKEN"), repo=os.environ.get("GITHUB_REPOSITORY"))
    if args.dry_run:
        agent.sync_to_filesystem(store.values())
    else:
        agent.sync(store.values())

    # Update overviews
    # In a more complete implementation we'd persist overview timestamps.
    overview_state = load_overview_state()
    overview_state["daily"] = update_overview(
        slack, SLACK_CH_DAILY, filter(lambda i: i.status == "daily", store.values()), overview_state.get("daily")
    )
    overview_state["weekly"] = update_overview(
        slack, SLACK_CH_WEEKLY, filter(lambda i: i.status == "weekly", store.values()), overview_state.get("weekly")
    )
    overview_state["monthly"] = update_overview(
        slack, SLACK_CH_MONTHLY, filter(lambda i: i.status == "monthly", store.values()), overview_state.get("monthly")
    )
    save_overview_state({k: v for k, v in overview_state.items() if v})


if __name__ == "__main__":
    main()
