# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Cosmos DB access for the agent: collection targets, candidates and logs."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from azure.core.credentials_async import AsyncTokenCredential
from azure.cosmos.aio import ContainerProxy, CosmosClient
from azure.cosmos.exceptions import CosmosResourceNotFoundError

from pa_shared.sources import COSMOS_CONTAINERS, new_source, normalize_target, source_id, utc_now

from .config import Settings

logger = logging.getLogger(__name__)

REPROPOSE_AFTER_DAYS = 90
LOG_TTL_SECONDS = 365 * 24 * 3600


def candidate_id(kind: str, target: str) -> str:
    """Deterministic candidate id so repeated proposals of the same target collapse into one item."""
    return "cand-" + source_id(kind, target).split("-", 1)[1]


def decide_proposal(existing: dict[str, Any] | None, source_registered: bool, now: datetime) -> str:
    """Decide what to do with a new proposal.

    Returns one of ``already_registered``, ``previously_rejected``, ``already_proposed``,
    ``repropose`` or ``create``.
    """
    if source_registered:
        return "already_registered"
    if existing is None:
        return "create"
    status = existing.get("status")
    if status == "approved":
        return "already_registered"
    if status == "proposed":
        return "already_proposed"
    decided = existing.get("decidedAt")
    if status == "rejected" and decided:
        if now - datetime.fromisoformat(decided) < timedelta(days=REPROPOSE_AFTER_DAYS):
            return "previously_rejected"
    return "repropose"


class Store:
    """Thin async repository over the Cosmos DB containers."""

    def __init__(self, settings: Settings, credential: AsyncTokenCredential) -> None:
        self._client = CosmosClient(settings.cosmos_endpoint, credential=credential)
        db = self._client.get_database_client(settings.cosmos_database)
        self._c: dict[str, ContainerProxy] = {k: db.get_container_client(v) for k, v in COSMOS_CONTAINERS.items()}

    async def close(self) -> None:
        await self._client.close()

    async def _query(self, container: str, query: str, params: list[dict[str, Any]] | None = None, limit: int = 50) -> list[dict[str, Any]]:
        items = []
        async for item in self._c[container].query_items(query, parameters=params or []):
            items.append({k: v for k, v in item.items() if not k.startswith("_")})
            if len(items) >= limit:
                break
        return items

    async def _read(self, container: str, item_id: str, pk: str) -> dict[str, Any] | None:
        try:
            return await self._c[container].read_item(item_id, partition_key=pk)
        except CosmosResourceNotFoundError:
            return None

    # -- sources -------------------------------------------------------------------------
    async def list_sources(self, include_disabled: bool = True) -> list[dict[str, Any]]:
        where = "" if include_disabled else " WHERE c.enabled = true"
        return await self._query(
            "sources",
            f"SELECT c.id, c.kind, c.target, c.name, c.enabled, c.tags, c.maxItems, c.origin, c.lastRun FROM c{where} ORDER BY c.name",
            limit=500,
        )

    async def get_source(self, sid: str) -> dict[str, Any] | None:
        return await self._read("sources", sid, sid)

    async def set_source_enabled(self, sid: str, enabled: bool) -> dict[str, Any] | None:
        doc = await self.get_source(sid)
        if doc is None:
            return None
        doc["enabled"] = enabled
        doc["updatedAt"] = utc_now()
        return await self._c["sources"].replace_item(sid, doc)

    async def list_recent_items(self, days: int, limit: int = 30) -> list[dict[str, Any]]:
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
        return await self._query(
            "items",
            "SELECT TOP @n c.sourceId, c.title, c.url, c.publishedAt, c.collectedAt, c.contentBlob FROM c "
            "WHERE c.collectedAt >= @since ORDER BY c.collectedAt DESC",
            [{"name": "@n", "value": limit}, {"name": "@since", "value": since}],
            limit=limit,
        )

    # -- candidates ----------------------------------------------------------------------
    async def propose_candidate(
        self, kind: str, target: str, name: str, reason: str, evidence: list[str], session_id: str | None
    ) -> dict[str, Any]:
        normalized = normalize_target(kind, target)
        cid = candidate_id(kind, normalized)
        existing = await self._read("candidates", cid, cid)
        registered = await self.get_source(source_id(kind, normalized)) is not None
        action = decide_proposal(existing, registered, datetime.now(UTC))
        now = utc_now()
        if action in ("already_registered", "previously_rejected", "already_proposed"):
            if action == "already_proposed" and existing is not None:
                existing["proposeCount"] = int(existing.get("proposeCount", 1)) + 1
                existing["lastProposedAt"] = now
                await self._c["candidates"].replace_item(cid, existing)
            return {"candidateId": cid, "result": action, "status": (existing or {}).get("status")}
        doc = {
            "id": cid,
            "kind": kind,
            "target": normalized,
            "name": name.strip() or normalized,
            "reason": reason.strip(),
            "evidence": [e for e in evidence if e][:10],
            "status": "proposed",
            "proposedAt": now,
            "lastProposedAt": now,
            "proposeCount": 1 if existing is None else int(existing.get("proposeCount", 1)) + 1,
            "sessionId": session_id,
            "history": (existing or {}).get("history", []) + ([{"status": existing["status"], "at": existing.get("decidedAt")}] if existing else []),
        }
        await self._c["candidates"].upsert_item(doc)
        return {"candidateId": cid, "result": "created" if action == "create" else "reproposed", "status": "proposed"}

    async def list_candidates(self, status: str | None) -> list[dict[str, Any]]:
        if status:
            return await self._query(
                "candidates",
                "SELECT c.id, c.kind, c.target, c.name, c.reason, c.status, c.proposedAt, c.decidedAt FROM c WHERE c.status = @s ORDER BY c.proposedAt DESC",
                [{"name": "@s", "value": status}],
            )
        return await self._query(
            "candidates",
            "SELECT c.id, c.kind, c.target, c.name, c.reason, c.status, c.proposedAt, c.decidedAt FROM c ORDER BY c.proposedAt DESC",
        )

    async def approve_candidate(self, cid: str, confirmation: str, tags: list[str], max_items: int) -> dict[str, Any]:
        cand = await self._read("candidates", cid, cid)
        if cand is None:
            return {"result": "not_found", "candidateId": cid}
        if cand["status"] != "proposed":
            return {"result": f"not_proposed (status={cand['status']})", "candidateId": cid}
        source = new_source(
            cand["kind"],
            cand["target"],
            cand["name"],
            tags=tags,
            max_items=max_items,
            origin={"type": "candidate", "candidateId": cid, "approvedAt": utc_now()},
        )
        await self._c["sources"].upsert_item(source)
        cand.update(
            {"status": "approved", "decidedAt": utc_now(), "decisionNote": confirmation.strip(), "registeredSourceId": source["id"]}
        )
        await self._c["candidates"].replace_item(cid, cand)
        return {"result": "approved", "candidateId": cid, "sourceId": source["id"], "kind": source["kind"], "target": source["target"]}

    async def reject_candidate(self, cid: str, reason: str) -> dict[str, Any]:
        cand = await self._read("candidates", cid, cid)
        if cand is None:
            return {"result": "not_found", "candidateId": cid}
        if cand["status"] != "proposed":
            return {"result": f"not_proposed (status={cand['status']})", "candidateId": cid}
        cand.update({"status": "rejected", "decidedAt": utc_now(), "decisionNote": reason.strip()})
        await self._c["candidates"].replace_item(cid, cand)
        return {"result": "rejected", "candidateId": cid}

    # -- logs ----------------------------------------------------------------------------
    async def add_conversation_turn(self, session_id: str, user_text: str, response_text: str, tool_calls: list[dict[str, Any]]) -> None:
        await self._c["conversations"].upsert_item(
            {
                "id": str(uuid.uuid4()),
                "sessionId": session_id,
                "timestamp": utc_now(),
                "userText": user_text[:8000],
                "responseText": response_text[:16000],
                "toolCalls": tool_calls[:30],
                "ttl": LOG_TTL_SECONDS,
            }
        )

    async def add_web_search_log(self, session_id: str, user_text: str, queries: list[str], urls: list[dict[str, str]]) -> None:
        await self._c["searchlogs"].upsert_item(
            {
                "id": str(uuid.uuid4()),
                "sessionId": session_id,
                "timestamp": utc_now(),
                "userText": user_text[:2000],
                "queries": queries[:20],
                "urls": urls[:50],
                "ttl": LOG_TTL_SECONDS,
            }
        )

    async def recent_history(self, days: int, limit: int = 40) -> dict[str, list[dict[str, Any]]]:
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
        params = [{"name": "@since", "value": since}, {"name": "@n", "value": limit}]
        conversations = await self._query(
            "conversations",
            "SELECT TOP @n c.timestamp, c.userText, c.toolCalls FROM c WHERE c.timestamp >= @since ORDER BY c.timestamp DESC",
            params,
            limit=limit,
        )
        searches = await self._query(
            "searchlogs",
            "SELECT TOP @n c.timestamp, c.queries, c.urls FROM c WHERE c.timestamp >= @since ORDER BY c.timestamp DESC",
            params,
            limit=limit,
        )
        return {"conversations": conversations, "webSearches": searches}
