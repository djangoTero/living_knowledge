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

# ValueScore ağırlıkları
W_ACC, W_ENG, W_PIN, W_AGE = 0.5, 0.3, 0.1, 0.1
PROMOTE_WEEKLY_SCORE = 0.75
PROMOTE_MONTHLY_SCORE = 0.85


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

    def fetch_metrics(self, channel: str, ts: str) -> Dict[str, int | bool]:
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


def value_score(item: NewsItem, now: dt.datetime) -> float:
    acc = float(item.accuracy)
    eng = min(item.replies / 5, 1.0)
    pin = 1.0 if item.pinned else 0.0
    age_h = max((now - parse_datetime(item.published_utc)).total_seconds() / 3600, 0)
    age_factor = max(0.0, 1.0 - min(age_h / 24, 1.0))
    score = W_ACC * acc + W_ENG * eng + W_PIN * pin + W_AGE * age_factor
    return round(float(score), 3)


def dynamic_ttl_hours(base_hours: int, score: float) -> int:
    if score >= 0.8:
        return int(base_hours * 1.5)
    if score < 0.5:
        return int(base_hours * 0.5)
    return base_hours


def should_promote_weekly(item: NewsItem, now: dt.datetime) -> bool:
    published = parse_datetime(item.published_utc)
    age_hours = (now - published).total_seconds() / 3600
    if age_hours < PROMOTE_WEEKLY_HOURS:
        return False
    if item.accuracy < 0.7:
        return False
    return item.value_score >= PROMOTE_WEEKLY_SCORE or item.corroborations >= 2 or item.replies >= 3 or item.pinned


def should_promote_monthly(item: NewsItem, now: dt.datetime) -> bool:
    published = parse_datetime(item.published_utc)
    age_hours = (now - published).total_seconds() / 3600
    if age_hours < PROMOTE_MONTHLY_HOURS:
        return False
    if item.accuracy < 0.8:
        return False
    return item.value_score >= PROMOTE_MONTHLY_SCORE or item.corroborations >= 3 or item.replies >= 5


def slack_ts
