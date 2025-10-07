#!/usr/bin/env python3
"""GitHub agent that mirrors the news state into Markdown content."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import pathlib
from typing import Dict, Iterable, List, Optional

import requests

from common import NewsItem, StateStore, configure_logging, group_by_status, write_file

GITHUB_API = "https://api.github.com"
DEFAULT_BRANCH = os.environ.get("GITHUB_BASE_BRANCH", "work")
PR_BRANCH_PREFIX = os.environ.get("PR_BRANCH_PREFIX", "auto/ai-news/")
NEWS_DIR = pathlib.Path("news")
INDEX_PATH = NEWS_DIR / "index.md"


def build_markdown(item: NewsItem) -> str:
    front_matter = {
        "id": item.id,
        "url": item.url,
        "title": item.title,
        "source": item.source,
        "published_utc": item.published_utc,
        "status": item.status,
        "accuracy": item.accuracy,
        "corroborations": item.corroborations,
        "meaning": item.meaning,
        "impact": item.impact,
        "affected": item.affected,
        "ts_daily": item.ts_daily,
        "ts_weekly": item.ts_weekly,
        "ts_monthly": item.ts_monthly,
    }
    header_lines = ["---"]
    for key, value in front_matter.items():
        header_lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    header_lines.append("---\n")
    body = f"## {item.title}\n\n- Source: {item.source}\n- Accuracy: {item.accuracy}\n\n{item.meaning}\n"
    return "\n".join(header_lines) + body


def render_index(groups: Dict[str, List[NewsItem]]) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    lines = ["# AI News Digest", "", f"_Updated {now.isoformat()}_", ""]
    order = ["daily", "weekly", "monthly"]
    titles = {"daily": "Daily Highlights", "weekly": "Weekly Spotlight", "monthly": "Monthly Archive"}
    for status in order:
        lines.append(f"## {titles[status]}")
        lines.append("")
        for item in groups.get(status, []):
            link = f"[{item.title}](./{status}/{item.id}.md)"
            lines.append(f"- {link} — {item.source} (accuracy {item.accuracy})")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


class GitHubAgent:
    def __init__(self, token: Optional[str], repo: Optional[str]) -> None:
        self.token = token
        self.repo = repo
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "ai-news-pipeline/1.0"})
        if token:
            self.session.headers.update({"Authorization": f"Bearer {token}"})

    # Local sync helpers -------------------------------------------------
    def sync_to_filesystem(self, items: Iterable[NewsItem]) -> None:
        groups = group_by_status(items)
        for status, bucket in groups.items():
            folder = NEWS_DIR / status
            folder.mkdir(parents=True, exist_ok=True)
            existing_files = {p.name for p in folder.glob("*.md")}
            target_files = set()
            for item in bucket:
                path = folder / f"{item.id}.md"
                write_file(path, build_markdown(item))
                target_files.add(path.name)
            for filename in existing_files - target_files:
                (folder / filename).unlink()
        index_content = render_index(groups)
        write_file(INDEX_PATH, index_content)

    def sync(self, items: Iterable[NewsItem]) -> None:
        groups = group_by_status(items)
        files: Dict[str, str] = {}
        for status, bucket in groups.items():
            for item in bucket:
                path = f"news/{status}/{item.id}.md"
                files[path] = build_markdown(item)
        files[str(INDEX_PATH)] = render_index(groups)
        changed = False
        for path, content in files.items():
            local_path = pathlib.Path(path)
            if not local_path.exists() or local_path.read_text(encoding="utf-8") != content:
                changed = True
                break
        self.sync_to_filesystem(items)
        if not self.token:
            logging.info("GitHub token not provided; skipping PR")
            return
        if not changed:
            logging.info("No content changes detected; skipping PR")
            return
        branch_suffix = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S")
        title = "[codex] Sync AI news"
        body = "Automated sync of AI news content."
        self.open_pr(branch_suffix, title, body, files)

    # GitHub API helpers -------------------------------------------------
    def _ensure_repo(self) -> None:
        if not self.repo:
            raise RuntimeError("GITHUB_REPOSITORY not set; cannot use GitHub API mode")
        if not self.token:
            raise RuntimeError("GITHUB_TOKEN not set; cannot use GitHub API mode")

    def _request(self, method: str, path: str, **kwargs):
        url = f"{GITHUB_API}{path}"
        response = self.session.request(method, url, timeout=30, **kwargs)
        if response.status_code >= 400:
            raise RuntimeError(f"GitHub API error {response.status_code}: {response.text}")
        if response.text:
            return response.json()
        return None

    def open_pr(self, branch_suffix: str, title: str, body: str, files: Dict[str, str]) -> None:
        self._ensure_repo()
        branch_name = f"{PR_BRANCH_PREFIX}{branch_suffix}"
        logging.info("Preparing PR %s", branch_name)
        # Fetch default branch sha
        ref = self._request("GET", f"/repos/{self.repo}/git/ref/heads/{DEFAULT_BRANCH}")
        base_sha = ref["object"]["sha"]
        # Create branch
        try:
            self._request(
                "POST",
                f"/repos/{self.repo}/git/refs",
                json={"ref": f"refs/heads/{branch_name}", "sha": base_sha},
            )
        except RuntimeError as exc:
            if "Reference already exists" not in str(exc):
                raise
        # Create tree
        tree_entries = []
        for path, content in files.items():
            tree_entries.append({"path": path, "mode": "100644", "type": "blob", "content": content})
        tree = self._request(
            "POST",
            f"/repos/{self.repo}/git/trees",
            json={"base_tree": base_sha, "tree": tree_entries},
        )
        # Create commit
        commit = self._request(
            "POST",
            f"/repos/{self.repo}/git/commits",
            json={"message": title, "tree": tree["sha"], "parents": [base_sha]},
        )
        # Update branch ref
        self._request(
            "PATCH",
            f"/repos/{self.repo}/git/refs/heads/{branch_name}",
            json={"sha": commit["sha"]},
        )
        # Open PR
        self._request(
            "POST",
            f"/repos/{self.repo}/pulls",
            json={"title": title, "head": branch_name, "base": DEFAULT_BRANCH, "body": body},
        )
        logging.info("Opened PR %s", branch_name)


def run_agent(dry_run: bool) -> None:
    store = StateStore()
    store.load()
    agent = GitHubAgent(token=os.environ.get("GITHUB_TOKEN"), repo=os.environ.get("GITHUB_REPOSITORY"))
    if dry_run or not agent.token:
        logging.info("Running in filesystem sync mode")
        agent.sync_to_filesystem(store.values())
    else:
        logging.info("Running in GitHub PR mode")
        groups = group_by_status(store.values())
        files: Dict[str, str] = {}
        for status, bucket in groups.items():
            for item in bucket:
                path = f"news/{status}/{item.id}.md"
                files[path] = build_markdown(item)
        files[str(INDEX_PATH)] = render_index(groups)
        branch_suffix = dt.datetime.utcnow().strftime("%Y%m%d%H%M%S")
        title = "[codex] Sync AI news"
        body = "Automated sync of AI news content."
        agent.open_pr(branch_suffix, title, body, files)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    configure_logging()
    run_agent(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
