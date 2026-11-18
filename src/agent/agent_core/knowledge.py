# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Foundry IQ (Azure AI Search agentic retrieval) client used by the agent tools.

The retrieve REST API is called directly so that per-request controls such as the topic graph
mode, reasoning effort and filters can be set (the built-in context provider exposes fixed options).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

import httpx
from azure.core.credentials_async import AsyncTokenCredential

from .config import Settings

logger = logging.getLogger(__name__)

SEARCH_SCOPE = "https://search.azure.com/.default"
Effort = Literal["minimal", "low", "medium"]
TopicMode = Literal["balanced", "diverse"]

MAX_TEXT_CHARS = 24_000
MAX_SOURCE_DATA_CHARS = 1_500


class KnowledgeError(RuntimeError):
    """Raised when Foundry IQ returns an error."""


def _clip(value: Any, limit: int) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "…"
    return value


def summarize_references(references: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reduce reference objects to the fields useful for citing and follow-up reads."""
    summarized = []
    for ref in references:
        source_data = ref.get("sourceData") or {}
        item: dict[str, Any] = {
            "ref_id": ref.get("id"),
            "type": ref.get("type"),
            "score": ref.get("rerankerScore"),
        }
        for key in ("docKey", "blobUrl", "url", "title"):
            if ref.get(key):
                item[key] = ref[key]
        if isinstance(source_data, dict):
            item["sourceData"] = {k: _clip(v, MAX_SOURCE_DATA_CHARS) for k, v in source_data.items() if v not in (None, "", [])}
        summarized.append(item)
    return summarized


def format_retrieval(result: dict[str, Any], *, mode: str) -> str:
    """Turn a retrieve response into compact JSON for the model."""
    texts = [
        content.get("text", "")
        for message in result.get("response", [])
        for content in message.get("content", [])
        if content.get("type") == "text"
    ]
    activity = [
        {k: a.get(k) for k in ("type", "knowledgeSourceName", "count", "elapsedMs", "warning") if a.get(k) is not None}
        for a in result.get("activity", [])
    ]
    payload = {
        "mode": mode,
        "extracted": _clip("\n".join(texts), MAX_TEXT_CHARS),
        "references": summarize_references(result.get("references", [])),
        "activity": activity,
    }
    return json.dumps(payload, ensure_ascii=False)


class KnowledgeClient:
    """Calls the knowledge base retrieve endpoints of Foundry IQ."""

    def __init__(self, settings: Settings, credential: AsyncTokenCredential) -> None:
        self._settings = settings
        self._credential = credential
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0))

    async def close(self) -> None:
        await self._http.aclose()

    async def _post(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        token = await self._credential.get_token(SEARCH_SCOPE)
        resp = await self._http.post(url, json=body, headers={"Authorization": f"Bearer {token.token}"})
        if resp.status_code not in (200, 206):
            raise KnowledgeError(f"Foundry IQ retrieve failed ({resp.status_code}): {resp.text[:500]}")
        return resp.json()

    def _messages(self, query: str) -> list[dict[str, Any]]:
        return [{"role": "user", "content": [{"type": "text", "text": query}]}]

    async def search(self, query: str, *, effort: Effort = "low", max_documents: int = 10) -> str:
        """Normal (precise) search over the collected content."""
        s = self._settings
        body: dict[str, Any] = {
            "retrievalReasoningEffort": {"kind": effort},
            "outputMode": "extractiveData",
            "includeActivity": True,
            "maxOutputDocuments": max(1, min(max_documents, 50)),
            "maxRuntimeInSeconds": 60,
            "knowledgeSourceParams": [
                {
                    "knowledgeSourceName": s.knowledge_source,
                    "kind": "azureBlob",
                    "alwaysQuerySource": True,
                    "includeReferences": True,
                    "includeReferenceSourceData": True,
                }
            ],
        }
        if effort == "minimal":
            body["intents"] = [{"type": "semantic", "search": query}]
        else:
            body["messages"] = self._messages(query)
        url = f"{s.search_endpoint}/knowledgebases('{s.knowledge_base}')/retrieve?api-version={s.search_api_version}"
        return format_retrieval(await self._post(url, body), mode="search")

    async def explore(self, query: str, *, topic_mode: TopicMode = "diverse", diversity: float = 0.3, max_documents: int = 20) -> str:
        """Broad exploration across related topics using the topic graph (preview).

        Falls back to a wide normal search when the topic graph is not configured, so the rest of
        the agent keeps working while the graph part is verified independently.
        """
        s = self._settings
        if not s.topic_graph_enabled:
            result = json.loads(await self.search(query, effort="medium", max_documents=max(max_documents, 20)))
            result["mode"] = "explore-fallback"
            result["note"] = "Topic graph is not configured; a broad normal search was used instead."
            return json.dumps(result, ensure_ascii=False)

        topic_graph: dict[str, Any] = {"kind": topic_mode, "minDiversityPoolSize": 1000}
        if topic_mode == "diverse":
            topic_graph["diversityFactor"] = max(0.0, min(diversity, 1.0))
        body = {
            "messages": self._messages(query),
            "retrievalReasoningEffort": {"kind": "low"},
            "outputMode": "extractiveData",
            "includeActivity": True,
            "maxOutputDocuments": max(1, min(max_documents, 50)),
            "maxRuntimeInSeconds": 90,
            "knowledgeSourceParams": [
                {
                    "knowledgeSourceName": s.topic_knowledge_source,
                    "kind": "searchIndex",
                    "alwaysQuerySource": True,
                    "includeReferences": True,
                    "includeReferenceSourceData": True,
                    "topicGraph": topic_graph,
                }
            ],
        }
        url = f"{s.search_endpoint}/knowledgebases('{s.topic_knowledge_base}')/retrieve?api-version={s.search_preview_api_version}"
        return format_retrieval(await self._post(url, body), mode=f"explore-{topic_mode}")
