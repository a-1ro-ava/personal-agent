# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Collection target definitions shared by the agent, the collector function and scripts.

Source of truth: src/shared/pa_shared. Copies under src/agent and src/functions are refreshed by
``uv run scripts/sync_shared.py`` (also run by the predeploy hook); do not edit the copies.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Any, Literal, get_args
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SourceKind = Literal["rss", "web", "github_releases", "arxiv"]
SOURCE_KINDS: tuple[str, ...] = get_args(SourceKind)

COSMOS_CONTAINERS = {
    "sources": "sources",
    "items": "items",
    "runs": "runs",
    "conversations": "conversations",
    "searchlogs": "searchlogs",
    "candidates": "candidates",
}

_TRACKING_PARAMS = re.compile(r"^(utm_[a-z]+|fbclid|gclid|mc_cid|mc_eid|ref|ref_src)$", re.IGNORECASE)
_GITHUB_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_DEFAULT_PORTS = {"http": 80, "https": 443}


class InvalidSourceError(ValueError):
    """Raised when a collection target is malformed or unsupported."""


def utc_now() -> str:
    """Return the current UTC time in ISO-8601 format."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def normalize_url(url: str) -> str:
    """Normalize an http(s) URL for de-duplication.

    Lower-cases scheme and host, drops default ports, fragments and tracking parameters,
    sorts the query string and removes a trailing slash from non-root paths.

    Raises:
        InvalidSourceError: If the URL is not an absolute http(s) URL.
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname:
        raise InvalidSourceError(f"Only absolute http(s) URLs are supported: {url!r}")
    host = parts.hostname.lower()
    netloc = host if parts.port in (None, _DEFAULT_PORTS[scheme]) else f"{host}:{parts.port}"
    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING_PARAMS.match(k)))
    return urlunsplit((scheme, netloc, path, query, ""))


def normalize_target(kind: str, target: str) -> str:
    """Normalize a collection target according to its kind.

    Raises:
        InvalidSourceError: If the kind is unknown or the target is malformed.
    """
    if kind not in SOURCE_KINDS:
        raise InvalidSourceError(f"Unknown source kind {kind!r}. Supported: {', '.join(SOURCE_KINDS)}")
    target = target.strip()
    if not target:
        raise InvalidSourceError("Target must not be empty.")
    if kind in ("rss", "web"):
        return normalize_url(target)
    if kind == "github_releases":
        repo = target
        if target.lower().startswith(("http://", "https://")):
            parts = urlsplit(target)
            if (parts.hostname or "").lower() != "github.com":
                raise InvalidSourceError(f"Not a github.com repository URL: {target!r}")
            repo = "/".join(parts.path.strip("/").split("/")[:2])
        repo = repo.removesuffix(".git").strip("/")
        if not _GITHUB_REPO.match(repo):
            raise InvalidSourceError(f"GitHub target must be 'owner/repo': {target!r}")
        return repo.lower()
    return " ".join(target.split())


def source_id(kind: str, target: str) -> str:
    """Return a stable id for a (kind, target) pair."""
    normalized = normalize_target(kind, target)
    digest = hashlib.sha256(f"{kind}|{normalized}".encode()).hexdigest()[:16]
    return f"{kind}-{digest}"


def new_source(
    kind: str,
    target: str,
    name: str,
    *,
    tags: list[str] | None = None,
    max_items: int = 20,
    origin: dict[str, Any] | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    """Build a ``sources`` container document.

    Raises:
        InvalidSourceError: If the target is invalid.
    """
    normalized = normalize_target(kind, target)
    now = utc_now()
    return {
        "id": source_id(kind, normalized),
        "kind": kind,
        "target": normalized,
        "name": name.strip() or normalized,
        "enabled": enabled,
        "tags": sorted({t.strip() for t in (tags or []) if t.strip()}),
        "maxItems": max(1, min(int(max_items), 100)),
        "origin": origin or {"type": "manual"},
        "createdAt": now,
        "updatedAt": now,
    }
