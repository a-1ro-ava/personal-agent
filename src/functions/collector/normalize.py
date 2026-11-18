# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Normalization of collected items into searchable Markdown documents."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pa_shared.sources import normalize_url

MAX_BODY_CHARS = 100_000
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass
class CollectedItem:
    """One collected document (article, page, release or paper)."""

    url: str
    title: str
    body: str
    published_at: str | None = None
    authors: list[str] = field(default_factory=list)
    raw: bytes = b""
    raw_ext: str = "json"
    raw_content_type: str = "application/json"
    extra: dict[str, Any] = field(default_factory=dict)


def item_key(url: str) -> str:
    """Stable key for an item derived from its normalized URL."""
    try:
        normalized = normalize_url(url)
    except ValueError:
        normalized = url.strip()
    return hashlib.sha256(normalized.encode()).hexdigest()[:24]


def clean_text(text: str) -> str:
    """Remove control characters, collapse blank lines and cap the length."""
    text = _CONTROL_CHARS.sub("", text or "").replace("\r\n", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:MAX_BODY_CHARS]


def _yaml_value(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


def build_content_markdown(item: CollectedItem, source: dict[str, Any], collected_at: datetime) -> str:
    """Build the normalized Markdown document indexed by Foundry IQ.

    The title, URL, source and dates are repeated in the body so they survive chunking and appear
    in retrieval results for citations.
    """
    title = clean_text(item.title) or item.url
    front = {
        "title": title,
        "url": item.url,
        "source_id": source["id"],
        "source_name": source.get("name", ""),
        "source_kind": source["kind"],
        "published": item.published_at or "",
        "collected": collected_at.isoformat(timespec="seconds"),
        "tags": ",".join(source.get("tags", [])),
    }
    lines = ["---", *(f"{k}: {_yaml_value(str(v))}" for k, v in front.items()), "---", "", f"# {title}", ""]
    lines.append(f"- 出典: [{source.get('name', source['kind'])}]({item.url})")
    lines.append(f"- URL: {item.url}")
    if item.published_at:
        lines.append(f"- 公開日: {item.published_at}")
    if item.authors:
        lines.append(f"- 著者: {', '.join(item.authors[:10])}")
    lines += ["", clean_text(item.body)]
    return "\n".join(lines).strip() + "\n"


def content_hash(markdown_body: str) -> str:
    """Hash used to detect changed content (ignores the collected timestamp)."""
    stable = re.sub(r'^collected: ".*"$', "", markdown_body, flags=re.MULTILINE)
    return hashlib.sha256(stable.encode()).hexdigest()


def to_iso(value: Any) -> str | None:
    """Convert feedparser time structs / ISO strings to ISO-8601 (UTC)."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return datetime(*value[:6]).isoformat() + "Z"
    except (TypeError, ValueError):
        return None
