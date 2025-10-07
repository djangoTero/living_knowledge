"""Shared utilities for the AI news pipeline scripts."""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import logging
import os
import pathlib
from typing import Dict, Iterable, List, Optional
from urllib.parse import parse_qs, urlparse, urlunparse

import yaml

STATE_PATH = pathlib.Path(os.environ.get("STATE_PATH", "state/items.jsonl"))
DOMAIN_WEIGHTS_PATH = pathlib.Path(os.environ.get("DOMAIN_WEIGHTS_PATH", "domain_weights.yml"))


@dataclasses.dataclass
class NewsItem:
    id: str
    url: str
    title: str
    source: str
    published_utc: str
    status: str
    accuracy: float
    corroborations: int
    meaning: str
    impact: str
    affected: str
    ts_daily: Optional[str] = None
    ts_weekly: Optional[str] = None
    ts_monthly: Optional[str] = None
    replies: int = 0
    pinned: bool = False

    def to_json(self) -> Dict[str, object]:
        return dataclasses.asdict(self)

    @staticmethod
    def from_json(data: Dict[str, object]) -> "NewsItem":
        return NewsItem(**data)


class StateStore:
    def __init__(self, path: pathlib.Path = STATE_PATH) -> None:
        self.path = path
        self._items: Dict[str, NewsItem] = {}

    def load(self) -> None:
        self._items.clear()
        if not self.path.exists():
            logging.debug("State file %s does not exist; starting fresh", self.path)
            return
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                item = NewsItem.from_json(data)
                self._items[item.id] = item
        logging.info("Loaded %d items from state", len(self._items))

    def save(self) -> None:
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        with temp_path.open("w", encoding="utf-8") as f:
            for item in self._items.values():
                json.dump(item.to_json(), f, ensure_ascii=False)
                f.write("\n")
        temp_path.replace(self.path)
        logging.info("Persisted %d items to %s", len(self._items), self.path)

    def upsert(self, item: NewsItem) -> None:
        self._items[item.id] = item

    def get(self, item_id: str) -> Optional[NewsItem]:
        return self._items.get(item_id)

    def values(self) -> Iterable[NewsItem]:
        return list(self._items.values())

    def remove(self, item_id: str) -> None:
        if item_id in self._items:
            del self._items[item_id]


def configure_logging() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")


def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    params = parse_qs(parsed.query)
    for key in ("id", "p", "story", "article", "aid"):
        values = params.get(key)
        if values:
            path = f"{path}/canonical-{key}-{values[0]}"
            break
    normalized = urlunparse((scheme, netloc, path, "", "", ""))
    return normalized


def make_item_id(url: str) -> str:
    normalized = normalize_url(url)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def parse_datetime(value: str) -> dt.datetime:
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        pass
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return dt.datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise ValueError(f"Unsupported datetime format: {value}")


def to_utc(dt_value: dt.datetime) -> dt.datetime:
    if dt_value.tzinfo is None:
        return dt_value.replace(tzinfo=dt.timezone.utc)
    return dt_value.astimezone(dt.timezone.utc)


def load_domain_weights(path: pathlib.Path = DOMAIN_WEIGHTS_PATH) -> Dict[str, float]:
    default_weight = 0.5
    weights: Dict[str, float] = {}
    if not path.exists():
        logging.warning("Domain weights file %s not found. Using default weight %s", path, default_weight)
        return {"__default__": default_weight}
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    default_weight = float(data.get("default", default_weight))
    for domain, weight in (data.get("weights") or {}).items():
        weights[domain.lower()] = float(weight)
    weights["__default__"] = default_weight
    return weights


def domain_weight_for(url: str, weights: Dict[str, float]) -> float:
    domain = urlparse(url).netloc.lower()
    while domain:
        if domain in weights:
            return weights[domain]
        if "." not in domain:
            break
        domain = domain.split(".", 1)[1]
    return weights.get("__default__", 0.5)


def compute_recency_score(published: dt.datetime, now: Optional[dt.datetime] = None, lookback_hours: int = 12) -> float:
    now = now or dt.datetime.now(dt.timezone.utc)
    delta = now - to_utc(published)
    hours = max(delta.total_seconds() / 3600, 0)
    if lookback_hours <= 0:
        return 0.0
    return max(0.0, 1.0 - min(hours / lookback_hours, 1.0))


def compute_accuracy(domain_weight: float, corroborations: int, recency: float) -> float:
    return round(domain_weight + corroborations + recency, 3)


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Environment variable {name} is required")
    return value


def ensure_dir(path: pathlib.Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def write_file(path: pathlib.Path, content: str) -> None:
    ensure_dir(path)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as f:
        f.write(content)
    temp_path.replace(path)


def group_by_status(items: Iterable[NewsItem]) -> Dict[str, List[NewsItem]]:
    buckets: Dict[str, List[NewsItem]] = {"daily": [], "weekly": [], "monthly": []}
    for item in items:
        buckets.setdefault(item.status, []).append(item)
    for bucket in buckets.values():
        bucket.sort(key=lambda x: x.published_utc, reverse=True)
    return buckets
