# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Tests for source normalization, candidate rules, report safety and activity logging."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from agent_framework import AgentResponse, Content, Message

from agent_core.activity_log import extract_activity
from agent_core.config import Settings
from agent_core.knowledge import KnowledgeClient, format_retrieval
from agent_core.reports import render_report_html
from agent_core.store import candidate_id, decide_proposal
from agent_core.tools import confirmation_matches
from pa_shared.sources import InvalidSourceError, new_source, normalize_target, normalize_url, source_id


def test_normalize_url_drops_tracking_fragment_and_trailing_slash():
    assert normalize_url("HTTPS://Example.com:443/Blog/?utm_source=x&b=2&a=1#top") == "https://example.com/Blog?a=1&b=2"


@pytest.mark.parametrize("target", ["Microsoft/Agent-Framework", "https://github.com/microsoft/agent-framework/releases", "microsoft/agent-framework.git"])
def test_github_targets_normalize_to_owner_repo(target: str):
    assert normalize_target("github_releases", target) == "microsoft/agent-framework"


@pytest.mark.parametrize(("kind", "target"), [("ftp", "x"), ("rss", "file:///etc/passwd"), ("github_releases", "https://gitlab.com/a/b")])
def test_invalid_targets_are_rejected(kind: str, target: str):
    with pytest.raises(InvalidSourceError):
        normalize_target(kind, target)


def test_source_and_candidate_ids_are_stable_across_spellings():
    a = new_source("rss", "https://example.com/feed/?utm_medium=y", "Feed")
    assert a["id"] == source_id("rss", "https://EXAMPLE.com/feed")
    assert candidate_id("rss", a["target"]) == candidate_id("rss", "https://example.com/feed/")


NOW = datetime(2026, 11, 18, tzinfo=UTC)


@pytest.mark.parametrize(
    ("existing", "registered", "expected"),
    [
        (None, False, "create"),
        (None, True, "already_registered"),
        ({"status": "proposed"}, False, "already_proposed"),
        ({"status": "approved"}, False, "already_registered"),
        ({"status": "rejected", "decidedAt": (NOW - timedelta(days=10)).isoformat()}, False, "previously_rejected"),
        ({"status": "rejected", "decidedAt": (NOW - timedelta(days=120)).isoformat()}, False, "repropose"),
    ],
)
def test_decide_proposal(existing, registered, expected):
    assert decide_proposal(existing, registered, NOW) == expected


def test_confirmation_must_quote_the_latest_user_message():
    said = "ありがとう。候補 cand-1234 を承認します。"
    assert confirmation_matches("cand-1234 を承認します", said)
    assert not confirmation_matches("承認します", "詳しく教えて")
    assert not confirmation_matches("", said)


def test_report_html_is_sanitized():
    page = render_report_html("T <x>", "# H\n\n<script>alert(1)</script>\n\n[link](javascript:alert(1)) [ok](https://example.com)")
    assert "<script>" not in page
    assert 'href="javascript:' not in page
    assert 'href="https://example.com"' in page and 'rel="noopener noreferrer nofollow"' in page
    assert "Content-Security-Policy" in page and "T &lt;x&gt;" in page


def test_extract_activity_collects_queries_and_cited_urls():
    response = AgentResponse(
        messages=[
            Message(
                role="assistant",
                contents=[
                    Content.from_search_tool_call("ws1", tool_name="web_search", arguments={"type": "search", "query": "MAF release"}),
                    Content.from_function_call("fc1", "search_knowledge", arguments={"query": "q"}),
                    Content.from_text(
                        "answer", annotations=[{"type": "citation", "url": "https://example.com/a", "title": "A"}]
                    ),
                ],
            )
        ]
    )
    activity = extract_activity(response)
    assert activity["queries"] == ["MAF release"]
    assert activity["urls"] == [{"url": "https://example.com/a", "title": "A"}]
    assert [c["name"] for c in activity["toolCalls"]] == ["web_search", "search_knowledge"]


def test_format_retrieval_keeps_text_and_reference_fields():
    out = json.loads(
        format_retrieval(
            {
                "response": [{"content": [{"type": "text", "text": "[{\"ref_id\":0}]"}]}],
                "references": [{"id": "0", "type": "azureBlob", "rerankerScore": 3.1, "docKey": "k", "sourceData": {"title": "T", "x": None}}],
                "activity": [{"type": "azureBlob", "count": 1, "elapsedMs": 10, "inputTokens": 5}],
            },
            mode="search",
        )
    )
    assert out["extracted"] == '[{"ref_id":0}]'
    assert out["references"][0] == {"ref_id": "0", "type": "azureBlob", "score": 3.1, "docKey": "k", "sourceData": {"title": "T"}}
    assert out["activity"] == [{"type": "azureBlob", "count": 1, "elapsedMs": 10}]


async def test_explore_falls_back_to_broad_search_without_topic_graph(monkeypatch: pytest.MonkeyPatch):
    settings = Settings.from_env().__class__(**{**Settings.from_env().__dict__, "search_endpoint": "https://s", "topic_knowledge_base": ""})
    client = KnowledgeClient(settings, credential=None)  # type: ignore[arg-type]
    calls = {}

    async def fake_search(query, *, effort, max_documents):
        calls.update(effort=effort, max_documents=max_documents)
        return json.dumps({"mode": "search", "extracted": "x", "references": [], "activity": []})

    monkeypatch.setattr(client, "search", fake_search)
    result = json.loads(await client.explore("trends", max_documents=5))
    await client.close()
    assert result["mode"] == "explore-fallback"
    assert calls == {"effort": "medium", "max_documents": 20}


@pytest.mark.parametrize(("mode", "expected"), [("diverse", {"kind": "diverse", "minDiversityPoolSize": 1000, "diversityFactor": 1.0}), ("balanced", {"kind": "balanced", "minDiversityPoolSize": 1000})])
async def test_explore_sends_per_request_topic_graph_to_topic_kb(monkeypatch: pytest.MonkeyPatch, mode, expected):
    base = Settings.from_env().__dict__
    settings = Settings(**{**base, "search_endpoint": "https://s", "topic_knowledge_base": "tkb", "topic_knowledge_source": "tks"})
    client = KnowledgeClient(settings, credential=None)  # type: ignore[arg-type]
    sent = {}

    async def fake_post(url, body):
        sent.update(url=url, body=body)
        return {"response": [], "references": [], "activity": []}

    monkeypatch.setattr(client, "_post", fake_post)
    result = json.loads(await client.explore("trends", topic_mode=mode, diversity=1.5))
    await client.close()
    [params] = sent["body"]["knowledgeSourceParams"]
    assert sent["url"] == f"https://s/knowledgebases('tkb')/retrieve?api-version={settings.search_preview_api_version}"
    assert (params["knowledgeSourceName"], params["kind"], params["topicGraph"]) == ("tks", "searchIndex", expected)
    assert result["mode"] == f"explore-{mode}"
