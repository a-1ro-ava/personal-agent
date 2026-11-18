# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Fetchers for each collection target kind (RSS/Atom, web page, GitHub releases, arXiv)."""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.robotparser
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, urlsplit

import feedparser
import httpx
import trafilatura
from bs4 import BeautifulSoup

from .normalize import CollectedItem, item_key, to_iso

logger = logging.getLogger(__name__)

USER_AGENT = "personal-agent-collector/0.1 (personal research use)"
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
RETRY_STATUS = {429, 500, 502, 503, 504}


class FetchError(RuntimeError):
    """Raised when a source cannot be fetched."""


class HttpFetcher:
    """HTTP client with robots.txt checks, retries and a response size cap."""

    def __init__(self, client: httpx.Client | None = None, attempts: int = 3, backoff: float = 2.0) -> None:
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
            transport=httpx.HTTPTransport(retries=2),
        )
        self._attempts = attempts
        self._backoff = backoff
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def close(self) -> None:
        self._client.close()

    def allowed(self, url: str) -> bool:
        """Honor robots.txt for page fetches. Unreachable robots.txt is treated as allowed."""
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            parser: urllib.robotparser.RobotFileParser | None = urllib.robotparser.RobotFileParser()
            try:
                resp = self._client.get(f"{origin}/robots.txt")
                if resp.status_code == 200:
                    parser.parse(resp.text.splitlines())
                else:
                    parser = None
            except httpx.HTTPError:
                parser = None
            self._robots[origin] = parser
        parser = self._robots[origin]
        return parser is None or parser.can_fetch(USER_AGENT, url)

    def get(self, url: str, headers: dict[str, str] | None = None, *, check_robots: bool = False) -> httpx.Response:
        if urlsplit(url).scheme not in ("http", "https"):
            raise FetchError(f"Unsupported URL scheme: {url}")
        if check_robots and not self.allowed(url):
            raise FetchError(f"Disallowed by robots.txt: {url}")
        last_error = ""
        for attempt in range(1, self._attempts + 1):
            try:
                resp = self._client.get(url, headers=headers)
            except httpx.HTTPError as e:
                last_error = f"{type(e).__name__}: {e}"
            else:
                if resp.status_code < 400:
                    if len(resp.content) > MAX_RESPONSE_BYTES:
                        raise FetchError(f"Response too large ({len(resp.content)} bytes): {url}")
                    return resp
                last_error = f"HTTP {resp.status_code}"
                if resp.status_code not in RETRY_STATUS:
                    break
            if attempt < self._attempts:
                time.sleep(self._backoff * attempt)
        raise FetchError(f"GET {url} failed: {last_error}")


def html_to_text(html: str, url: str | None = None) -> str:
    """Extract the main content of an HTML page as Markdown-ish text."""
    extracted = trafilatura.extract(
        html, url=url, output_format="markdown", include_links=True, include_tables=True, favor_recall=True
    )
    if extracted:
        return extracted
    return BeautifulSoup(html, "html.parser").get_text("\n", strip=True)


def html_title(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    return (soup.title.string or "").strip() if soup.title else ""


# --- parsers (pure, unit tested) --------------------------------------------------------------
def parse_feed(data: bytes, max_items: int) -> list[CollectedItem]:
    """Parse an RSS/Atom feed into items (body = entry summary/content)."""
    feed = feedparser.parse(data)
    items = []
    for entry in feed.entries[:max_items]:
        link = entry.get("link") or entry.get("id")
        if not link:
            continue
        content = entry.get("content") or []
        body_html = content[0].get("value", "") if content else entry.get("summary", "")
        body = BeautifulSoup(body_html, "html.parser").get_text("\n", strip=True) if body_html else ""
        items.append(
            CollectedItem(
                url=link,
                title=entry.get("title", ""),
                body=body,
                published_at=to_iso(entry.get("published_parsed") or entry.get("updated_parsed")),
                authors=[a.get("name", "") for a in entry.get("authors", []) if a.get("name")],
                raw=json.dumps(_plain(entry), ensure_ascii=False, default=str).encode(),
                extra={"tags": [t.get("term") for t in entry.get("tags", []) if t.get("term")]},
            )
        )
    return items


def parse_github_releases(data: bytes, max_items: int) -> list[CollectedItem]:
    """Parse the GitHub REST 'list releases' response."""
    releases = json.loads(data)
    items = []
    for rel in releases:
        if rel.get("draft"):
            continue
        name = rel.get("name") or rel.get("tag_name") or ""
        items.append(
            CollectedItem(
                url=rel["html_url"],
                title=name,
                body=rel.get("body") or "",
                published_at=rel.get("published_at"),
                authors=[rel.get("author", {}).get("login", "")] if rel.get("author") else [],
                raw=json.dumps(rel, ensure_ascii=False).encode(),
                extra={"tag": rel.get("tag_name"), "prerelease": rel.get("prerelease", False)},
            )
        )
        if len(items) >= max_items:
            break
    return items


def parse_arxiv(data: bytes, max_items: int) -> list[CollectedItem]:
    """Parse an arXiv API (Atom) response; the abstract becomes the body."""
    feed = feedparser.parse(data)
    items = []
    for entry in feed.entries[:max_items]:
        link = next((lnk.get("href") for lnk in entry.get("links", []) if lnk.get("rel") == "alternate"), entry.get("id"))
        if not link:
            continue
        categories = [t.get("term") for t in entry.get("tags", []) if t.get("term")]
        items.append(
            CollectedItem(
                url=link,
                title=" ".join(entry.get("title", "").split()),
                body=" ".join(entry.get("summary", "").split()) + (f"\n\nCategories: {', '.join(categories)}" if categories else ""),
                published_at=to_iso(entry.get("published_parsed")),
                authors=[a.get("name", "") for a in entry.get("authors", []) if a.get("name")],
                raw=json.dumps(_plain(entry), ensure_ascii=False, default=str).encode(),
                extra={"categories": categories},
            )
        )
    return items


def _plain(value: Any) -> Any:
    """Convert feedparser structures into JSON-serializable data."""
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items() if not k.endswith("_parsed")}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


# --- per-kind collection ----------------------------------------------------------------------
def collect_rss(http: HttpFetcher, source: dict[str, Any], is_new: Callable[[str], bool]) -> list[CollectedItem]:
    """Fetch the feed; for new entries also fetch the linked article to index its full text."""
    feed = http.get(source["target"])
    items = parse_feed(feed.content, source.get("maxItems", 20))
    for item in items:
        if not is_new(item_key(item.url)):
            continue
        try:
            page = http.get(item.url, check_robots=True)
        except FetchError as e:
            logger.info("full text skipped for %s: %s", item.url, e)
            continue
        text = html_to_text(page.text, item.url)
        if len(text) > len(item.body):
            item.extra["summary"] = item.body[:1000]
            item.body = text
            item.raw, item.raw_ext, item.raw_content_type = page.content, "html", "text/html"
    return items


def collect_web(http: HttpFetcher, source: dict[str, Any], is_new: Callable[[str], bool]) -> list[CollectedItem]:
    """Snapshot a single web page; updates are detected by content hash."""
    page = http.get(source["target"], check_robots=True)
    return [
        CollectedItem(
            url=source["target"],
            title=html_title(page.text) or source.get("name", ""),
            body=html_to_text(page.text, source["target"]),
            raw=page.content,
            raw_ext="html",
            raw_content_type="text/html",
        )
    ]


def collect_github_releases(http: HttpFetcher, source: dict[str, Any], is_new: Callable[[str], bool]) -> list[CollectedItem]:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    per_page = max(1, min(source.get("maxItems", 20), 100))
    resp = http.get(f"https://api.github.com/repos/{source['target']}/releases?per_page={per_page}", headers=headers)
    return parse_github_releases(resp.content, per_page)


def collect_arxiv(http: HttpFetcher, source: dict[str, Any], is_new: Callable[[str], bool]) -> list[CollectedItem]:
    n = max(1, min(source.get("maxItems", 20), 100))
    url = (
        "https://export.arxiv.org/api/query?search_query="
        + quote(source["target"], safe=":")
        + f"&sortBy=submittedDate&sortOrder=descending&max_results={n}"
    )
    return parse_arxiv(http.get(url).content, n)


COLLECTORS: dict[str, Callable[[HttpFetcher, dict[str, Any], Callable[[str], bool]], list[CollectedItem]]] = {
    "rss": collect_rss,
    "web": collect_web,
    "github_releases": collect_github_releases,
    "arxiv": collect_arxiv,
}
