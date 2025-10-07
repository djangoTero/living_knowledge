#!/usr/bin/env python3
"""Validates Markdown front matter for news items."""
from __future__ import annotations

import pathlib
import sys
from typing import Dict, List

import yaml

REQUIRED_FIELDS = {
    "id": str,
    "url": str,
    "title": str,
    "source": str,
    "published_utc": str,
    "status": str,
    "accuracy": (int, float),
    "corroborations": int,
    "meaning": str,
    "impact": str,
    "affected": str,
}
VALID_STATUSES = {"daily", "weekly", "monthly"}


def validate_file(path: pathlib.Path) -> List[str]:
    errors: List[str] = []
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        errors.append(f"{path}: missing YAML front matter")
        return errors
    try:
        _, header, _ = text.split("---\n", 2)
    except ValueError:
        errors.append(f"{path}: malformed YAML front matter")
        return errors
    data = yaml.safe_load(header)
    if not isinstance(data, dict):
        errors.append(f"{path}: front matter must be a mapping")
        return errors
    for field, expected in REQUIRED_FIELDS.items():
        if field not in data:
            errors.append(f"{path}: missing field {field}")
            continue
        if not isinstance(data[field], expected):
            errors.append(f"{path}: field {field} must be {expected}")
    status = data.get("status")
    if status and status not in VALID_STATUSES:
        errors.append(f"{path}: status {status} is invalid")
    return errors


def main() -> int:
    root = pathlib.Path("news")
    if not root.exists():
        return 0
    errors: List[str] = []
    for path in root.rglob("*.md"):
        errors.extend(validate_file(path))
    if errors:
        for line in errors:
            print(line)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
