# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Collection pipeline: sources (Cosmos DB) -> fetch -> Blob (raw + content) -> metadata -> Foundry IQ indexer."""

from __future__ import annotations

import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx
from azure.core.credentials import TokenCredential
from azure.cosmos import CosmosClient
from azure.cosmos.exceptions import CosmosHttpResponseError
from azure.storage.blob import BlobServiceClient, ContentSettings

from pa_shared.sources import COSMOS_CONTAINERS

from .fetchers import COLLECTORS, FetchError, HttpFetcher
from .normalize import build_content_markdown, content_hash, item_key

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9), "JST")
RUN_TTL_SECONDS = 180 * 24 * 3600
SEARCH_SCOPE = "https://search.azure.com/.default"
# Feed entries and arXiv abstracts are kept as first collected. Re-fetched feed entries only carry
# the summary, so comparing hashes would overwrite the full article text collected earlier.
IMMUTABLE_KINDS = {"rss", "arxiv"}


@dataclass(frozen=True)
class CollectorConfig:
    storage_account_url: str
    raw_container: str
    content_container: str
    cosmos_endpoint: str
    cosmos_database: str
    search_endpoint: str
    indexer_name: str
    search_api_version: str
    max_workers: int

    @classmethod
    def from_env(cls) -> CollectorConfig:
        ks = os.environ.get("KNOWLEDGE_SOURCE_NAME", "content-blob-ks")
        return cls(
            storage_account_url=os.environ["STORAGE_ACCOUNT_URL"],
            raw_container=os.environ.get("RAW_CONTAINER", "raw"),
            content_container=os.environ.get("CONTENT_CONTAINER", "content"),
            cosmos_endpoint=os.environ["COSMOS_ENDPOINT"],
            cosmos_database=os.environ.get("COSMOS_DATABASE", "personal-agent"),
            search_endpoint=os.environ.get("SEARCH_ENDPOINT", "").rstrip("/"),
            indexer_name=os.environ.get("SEARCH_INDEXER_NAME") or f"{ks}-indexer",
            search_api_version=os.environ.get("SEARCH_API_VERSION", "2026-10-01"),
            max_workers=int(os.environ.get("COLLECTOR_MAX_WORKERS", "4")),
        )


def summarize_counts(results: list[dict[str, Any]]) -> dict[str, int]:
    """Aggregate per-source results into run totals."""
    totals = {"sources": len(results), "failedSources": 0, "new": 0, "updated": 0, "unchanged": 0, "failedItems": 0}
    for r in results:
        if r.get("status") == "failed":
            totals["failedSources"] += 1
        for key in ("new", "updated", "unchanged", "failedItems"):
            totals[key] += int(r.get(key, 0))
    return totals


class Collector:
    """Runs one collection pass over the enabled sources."""

    def __init__(self, config: CollectorConfig, credential: TokenCredential) -> None:
        self._cfg = config
        self._cred = credential
        db = CosmosClient(config.cosmos_endpoint, credential=credential).get_database_client(config.cosmos_database)
        self._sources = db.get_container_client(COSMOS_CONTAINERS["sources"])
        self._items = db.get_container_client(COSMOS_CONTAINERS["items"])
        self._runs = db.get_container_client(COSMOS_CONTAINERS["runs"])
        self._blobs = BlobServiceClient(config.storage_account_url, credential=credential)

    def load_sources(self, source_ids: list[str] | None) -> list[dict[str, Any]]:
        if source_ids:
            return [s for s in (self._read_source(sid) for sid in source_ids) if s is not None]
        return list(self._sources.query_items("SELECT * FROM c WHERE c.enabled = true", enable_cross_partition_query=True))

    def _read_source(self, sid: str) -> dict[str, Any] | None:
        try:
            return self._sources.read_item(sid, partition_key=sid)
        except CosmosHttpResponseError:
            return None

    def run(self, source_ids: list[str] | None = None, trigger: str = "timer") -> dict[str, Any]:
        started = datetime.now(UTC)
        run_id = f"{started:%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
        sources = self.load_sources(source_ids)
        logger.info("run %s: %d source(s)", run_id, len(sources))

        with ThreadPoolExecutor(max_workers=max(1, self._cfg.max_workers)) as pool:
            results = list(pool.map(lambda s: self.collect_source(s, started), sources))

        totals = summarize_counts(results)
        indexer = {"status": "skipped", "reason": "no new or updated documents"}
        if totals["new"] or totals["updated"]:
            indexer = self.trigger_indexer()

        run_doc = {
            "id": run_id,
            "runDate": started.astimezone(JST).strftime("%Y-%m-%d"),
            "trigger": trigger,
            "startedAt": started.isoformat(timespec="seconds"),
            "finishedAt": datetime.now(UTC).isoformat(timespec="seconds"),
            "totals": totals,
            "sources": results,
            "indexer": indexer,
            "ttl": RUN_TTL_SECONDS,
        }
        self._runs.upsert_item(run_doc)
        logger.info("run %s finished: %s indexer=%s", run_id, totals, indexer.get("status"))
        return run_doc

    def collect_source(self, source: dict[str, Any], started: datetime) -> dict[str, Any]:
        sid = source["id"]
        result: dict[str, Any] = {"sourceId": sid, "kind": source["kind"], "new": 0, "updated": 0, "unchanged": 0, "failedItems": 0}
        existing = {
            doc["id"]: doc.get("contentHash")
            for doc in self._items.query_items("SELECT c.id, c.contentHash FROM c", partition_key=sid)
        }
        http = HttpFetcher()
        try:
            collect = COLLECTORS[source["kind"]]
            items = collect(http, source, lambda key: key not in existing)
        except (FetchError, KeyError, ValueError) as e:
            result.update({"status": "failed", "error": f"{type(e).__name__}: {e}"[:500]})
            self._update_source_last_run(sid, result, started)
            return result
        finally:
            http.close()

        for item in items:
            try:
                key = item_key(item.url)
                if source["kind"] in IMMUTABLE_KINDS and key in existing:
                    result["unchanged"] += 1
                    continue
                markdown = build_content_markdown(item, source, started)
                digest = content_hash(markdown)
                if existing.get(key) == digest:
                    result["unchanged"] += 1
                    continue
                self._store_item(source, key, item, markdown, digest, started, is_new=key not in existing)
                result["new" if key not in existing else "updated"] += 1
                existing[key] = digest
            except Exception as e:  # noqa: BLE001 - one bad item must not stop the source
                logger.warning("item failed (%s %s): %s", sid, item.url, e)
                result["failedItems"] += 1
        result["status"] = "succeeded" if not result["failedItems"] else "partial"
        self._update_source_last_run(sid, result, started)
        return result

    def _store_item(self, source: dict[str, Any], key: str, item, markdown: str, digest: str, started: datetime, *, is_new: bool) -> None:
        sid = source["id"]
        raw_name = f"{sid}/{started:%Y/%m/%d}/{key}.{item.raw_ext}"
        content_name = f"{sid}/{key}.md"
        if item.raw:
            self._blobs.get_blob_client(self._cfg.raw_container, raw_name).upload_blob(
                item.raw, overwrite=True, content_settings=ContentSettings(content_type=item.raw_content_type)
            )
        self._blobs.get_blob_client(self._cfg.content_container, content_name).upload_blob(
            markdown.encode("utf-8"),
            overwrite=True,
            content_settings=ContentSettings(content_type="text/markdown; charset=utf-8"),
        )
        now = started.isoformat(timespec="seconds")
        doc = {
            "id": key,
            "sourceId": sid,
            "sourceKind": source["kind"],
            "url": item.url,
            "title": item.title[:500],
            "publishedAt": item.published_at,
            "authors": item.authors[:20],
            "contentHash": digest,
            "rawBlob": f"{self._cfg.raw_container}/{raw_name}" if item.raw else None,
            "contentBlob": f"{self._cfg.content_container}/{content_name}",
            "updatedAt": now,
            "extra": item.extra,
        }
        if is_new:
            doc["collectedAt"] = now
            self._items.upsert_item(doc)
        else:
            prev = self._items.read_item(key, partition_key=sid)
            doc["collectedAt"] = prev.get("collectedAt", now)
            doc["revisions"] = int(prev.get("revisions", 0)) + 1
            self._items.replace_item(key, doc)

    def _update_source_last_run(self, sid: str, result: dict[str, Any], started: datetime) -> None:
        last_run = {k: v for k, v in result.items() if k != "sourceId"} | {"at": started.isoformat(timespec="seconds")}
        try:
            self._sources.patch_item(item=sid, partition_key=sid, patch_operations=[{"op": "set", "path": "/lastRun", "value": last_run}])
        except CosmosHttpResponseError as e:
            logger.warning("could not update lastRun for %s: %s", sid, e)

    def trigger_indexer(self) -> dict[str, Any]:
        """Start the Foundry IQ ingestion (indexer generated by the azureBlob knowledge source)."""
        cfg = self._cfg
        if not cfg.search_endpoint:
            return {"status": "skipped", "reason": "SEARCH_ENDPOINT not set"}
        url = f"{cfg.search_endpoint}/indexers('{cfg.indexer_name}')/run?api-version={cfg.search_api_version}"
        token = self._cred.get_token(SEARCH_SCOPE).token
        try:
            resp = httpx.post(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
        except httpx.HTTPError as e:
            return {"status": "failed", "error": str(e)[:300]}
        if resp.status_code in (200, 202):
            return {"status": "started", "indexer": cfg.indexer_name}
        if resp.status_code == 409:
            # Another run is in progress; it (or the next run) will pick up the new blobs.
            return {"status": "already_running", "indexer": cfg.indexer_name}
        return {"status": "failed", "code": resp.status_code, "error": resp.text[:300]}
