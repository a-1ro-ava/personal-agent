# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Tests for collector parsing, normalization and change detection."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from collector import pipeline
from collector.fetchers import parse_arxiv, parse_feed, parse_github_releases
from collector.normalize import CollectedItem, build_content_markdown, content_hash, item_key
from collector.pipeline import Collector, summarize_counts

RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Blog</title>
<item><title>First post</title><link>https://example.com/a?utm_source=x</link>
<pubDate>Tue, 17 Nov 2026 09:00:00 GMT</pubDate><description>&lt;p&gt;Hello &lt;b&gt;world&lt;/b&gt;&lt;/p&gt;</description></item>
<item><title>No link</title></item>
</channel></rss>"""

ARXIV = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry><id>http://arxiv.org/abs/2611.00001v1</id><published>2026-11-16T00:00:00Z</published>
<title>Agentic
  Retrieval</title><summary>We study   retrieval.</summary>
<author><name>A. Author</name></author>
<link href="http://arxiv.org/abs/2611.00001v1" rel="alternate" type="text/html"/>
<category term="cs.CL"/></entry></feed>"""

SOURCE = {"id": "rss-1", "kind": "rss", "name": "Blog", "tags": ["t"]}


def test_parse_feed_extracts_text_and_skips_entries_without_link():
    items = parse_feed(RSS, max_items=10)
    assert [i.title for i in items] == ["First post"]
    assert items[0].body == "Hello\nworld"
    assert items[0].published_at == "2026-11-17T09:00:00Z"


def test_parse_arxiv_uses_alternate_link_and_collapses_whitespace():
    [item] = parse_arxiv(ARXIV, max_items=5)
    assert item.url == "http://arxiv.org/abs/2611.00001v1"
    assert item.title == "Agentic Retrieval"
    assert item.body.startswith("We study retrieval.")
    assert "cs.CL" in item.body
    assert item.authors == ["A. Author"]


def test_parse_github_releases_skips_drafts_and_honors_limit():
    data = json.dumps(
        [
            {"draft": True, "html_url": "https://github.com/o/r/releases/tag/v3", "name": "v3"},
            {"html_url": "https://github.com/o/r/releases/tag/v2", "tag_name": "v2", "body": "notes", "published_at": "2026-11-01T00:00:00Z"},
            {"html_url": "https://github.com/o/r/releases/tag/v1", "name": "v1"},
        ]
    ).encode()
    items = parse_github_releases(data, max_items=1)
    assert [i.title for i in items] == ["v2"]


def test_item_key_ignores_tracking_parameters():
    assert item_key("https://example.com/a?utm_source=x") == item_key("https://EXAMPLE.com/a")


def test_content_hash_ignores_collection_time():
    item = CollectedItem(url="https://example.com/a", title="T", body="body")
    first = build_content_markdown(item, SOURCE, datetime(2026, 11, 17, tzinfo=UTC))
    second = build_content_markdown(item, SOURCE, datetime(2026, 11, 18, tzinfo=UTC))
    assert first != second
    assert content_hash(first) == content_hash(second)
    assert "- URL: https://example.com/a" in first


def test_summarize_counts():
    totals = summarize_counts([{"status": "failed"}, {"status": "succeeded", "new": 2, "updated": 1, "unchanged": 3}])
    assert totals == {"sources": 2, "failedSources": 1, "new": 2, "updated": 1, "unchanged": 3, "failedItems": 0}


class _FakeItems:
    def __init__(self, existing: dict[str, str]) -> None:
        self.existing = existing

    def query_items(self, query: str, partition_key: str) -> list[dict[str, Any]]:
        return [{"id": k, "contentHash": v} for k, v in self.existing.items()]


def _collector(existing: dict[str, str], monkeypatch: pytest.MonkeyPatch, items: list[CollectedItem]) -> tuple[Collector, list[str]]:
    collector = Collector.__new__(Collector)
    collector._items = _FakeItems(existing)  # type: ignore[attr-defined]
    stored: list[str] = []
    monkeypatch.setattr(collector, "_store_item", lambda source, key, *a, **k: stored.append(key), raising=False)
    monkeypatch.setattr(collector, "_update_source_last_run", lambda *a: None, raising=False)
    monkeypatch.setitem(pipeline.COLLECTORS, "rss", lambda http, source, is_new: items)
    monkeypatch.setitem(pipeline.COLLECTORS, "web", lambda http, source, is_new: items)
    return collector, stored


def test_existing_feed_entries_are_not_overwritten(monkeypatch: pytest.MonkeyPatch):
    item = CollectedItem(url="https://example.com/a", title="T", body="summary only")
    collector, stored = _collector({item_key(item.url): "hash-of-full-text"}, monkeypatch, [item])
    result = collector.collect_source(SOURCE, datetime.now(UTC))
    assert (result["unchanged"], result["updated"], stored) == (1, 0, [])


def test_changed_web_page_is_updated(monkeypatch: pytest.MonkeyPatch):
    item = CollectedItem(url="https://example.com/page", title="T", body="new text")
    collector, stored = _collector({item_key(item.url): "old-hash"}, monkeypatch, [item])
    result = collector.collect_source({**SOURCE, "kind": "web"}, datetime.now(UTC))
    assert (result["updated"], result["status"], stored) == (1, "succeeded", [item_key(item.url)])
