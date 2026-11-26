# Copyright (c) 2026 a-1ro-ava
# SPDX-License-Identifier: MIT
"""Tests for source normalization, candidate rules, report safety and activity logging."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from agent_framework import AgentResponse, AgentSession, Content, FunctionInvocationContext, Message

from agent_core.activity_log import LAST_USER_TEXT_KEY, extract_activity
from agent_core.config import Settings
from agent_core.history import drop_unpaired_tool_calls
from agent_core.illustrations import IllustrationError, IllustrationGenerator, account_openai_base_url, illustration_requested
from agent_core.knowledge import KnowledgeClient, format_retrieval
from agent_core.reports import normalize_image_blob_name, render_report_html
from agent_core.store import candidate_id, decide_proposal
from agent_core.tools import Services, build_tools, confirmation_matches
from agent_core.web_research import WebResearcher, WebResearchError, is_rate_limited
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


def test_unpaired_tool_calls_from_truncated_history_are_dropped():
    # The history window starts at an output whose call was cut off; the last call never got a result.
    messages = [
        Message(role="tool", contents=[Content.from_function_result("cut", result="old")]),
        Message(role="assistant", contents=[Content.from_text("earlier answer")]),
        Message(role="assistant", contents=[Content.from_function_call("ok", "search_knowledge", arguments="{}")]),
        Message(role="tool", contents=[Content.from_function_result("ok", result="hit")]),
        Message(
            role="assistant",
            contents=[Content.from_text("partial"), Content.from_function_call("dangling", "list_sources", arguments="{}")],
        ),
        Message(role="user", contents=[Content.from_text("next question")]),
    ]
    repaired = drop_unpaired_tool_calls(messages)
    assert [[(c.type, c.call_id) for c in m.contents] for m in repaired] == [
        [("text", None)],
        [("function_call", "ok")],
        [("function_result", "ok")],
        [("text", None)],
        [("text", None)],
    ]


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


class _RateLimit(Exception):
    status_code = 429


class _FlakyAgent:
    def __init__(self, failures: list[Exception]) -> None:
        self.failures = failures
        self.calls = 0

    async def run(self, prompt: str) -> AgentResponse:
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return AgentResponse(messages=[Message(role="assistant", contents=[Content.from_text("ok")])])


def test_rate_limit_is_detected_through_the_cause_chain():
    try:
        try:
            raise _RateLimit("x")
        except _RateLimit as inner:
            raise RuntimeError("service failed to complete the prompt") from inner
    except RuntimeError as outer:
        assert is_rate_limited(outer)
    assert is_rate_limited(Exception("Model deployment rate limit exceeded."))
    assert not is_rate_limited(ValueError("bad request"))


async def test_web_research_retries_only_rate_limits(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("agent_core.web_research.asyncio.sleep", _no_sleep)
    researcher = WebResearcher(client=None, max_attempts=3)  # type: ignore[arg-type]

    flaky = _FlakyAgent([_RateLimit("429"), _RateLimit("429")])
    assert (await researcher._run_with_retry(flaky, "q")).text == "ok"  # type: ignore[arg-type]
    assert flaky.calls == 3

    exhausted = _FlakyAgent([_RateLimit("429")] * 3)
    with pytest.raises(WebResearchError):
        await researcher._run_with_retry(exhausted, "q")  # type: ignore[arg-type]
    assert exhausted.calls == 3

    broken = _FlakyAgent([ValueError("bad request")])
    with pytest.raises(WebResearchError):
        await researcher._run_with_retry(broken, "q")  # type: ignore[arg-type]
    assert broken.calls == 1


async def _no_sleep(_: float) -> None:
    return None


@pytest.mark.parametrize(
    "text",
    [
        "さっきの調査内容を絵にして",
        "これ、絵で説明してもらえる？",
        "要点をイラストにまとめて",
        "今の結果を図解して",
        "画像にしてほしい",
        "全体像を描いてください",
        "Can you turn this into an infographic?",
        "Please draw it",
    ],
)
def test_explicit_picture_requests_are_detected(text: str):
    assert illustration_requested(text)


@pytest.mark.parametrize(
    "text",
    [
        "画像生成AIの最新動向をまとめて",
        "gpt-image-2 のリリース情報を調べて",
        "絵画のトレンドを教えて",
        "絵文字の使い方は？",
        "ビジョンを描いている企業を探して",
        "図解で学ぶ機械学習という本の評判は？",
        "絵にしないで文章でまとめて",
        "image generation news this week",
        "",
    ],
)
def test_other_messages_do_not_request_pictures(text: str):
    assert not illustration_requested(text)


class _FakeImages:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        return type("R", (), {"data": [type("D", (), {"b64_json": "iVBORw0K"})()]})()


class _FakeWriter:
    async def save_image(self, title: str, png: bytes) -> dict:
        return {"blob": "artifacts/images/2026/11/26/x.png", "size_bytes": len(png)}


def _illustration_tool(images: _FakeImages):
    client = type("C", (), {"images": images})()
    settings = Settings(**{**Settings.from_env().__dict__, "storage_account_url": "", "cosmos_endpoint": "", "search_endpoint": ""})
    illustrator = IllustrationGenerator(client, _FakeWriter(), "gpt-image-2")  # type: ignore[arg-type]
    services = Services(settings=settings, knowledge=None, store=None, reports=None, researcher=None, illustrator=illustrator)
    [tool_fn] = [t for t in build_tools(services) if getattr(t, "name", "") == "generate_illustration"]
    return tool_fn


async def _invoke_with_user_text(tool_fn, user_text: str) -> dict:
    session = AgentSession(session_id="s1")
    session.state[LAST_USER_TEXT_KEY] = user_text
    args = {"prompt": "要点A→要点B", "title": "T"}
    ctx = FunctionInvocationContext(function=tool_fn, arguments=args, session=session)
    result = await tool_fn.invoke(arguments=args, context=ctx)
    text = result if isinstance(result, str) else "".join(c.text or "" for c in result)
    return json.loads(text)


async def test_illustration_tool_only_generates_on_explicit_request():
    images = _FakeImages()
    tool_fn = _illustration_tool(images)

    assert (await _invoke_with_user_text(tool_fn, "最新のエージェント動向を調べて"))["result"] == "not_requested"
    assert images.calls == []

    generated = await _invoke_with_user_text(tool_fn, "調査結果を絵にして")
    assert generated["result"] == "generated" and generated["blob"].endswith(".png")
    assert [(c["model"], c["size"], c["n"]) for c in images.calls] == [("gpt-image-2", "1536x1024", 1)]


async def test_illustration_rate_limit_is_retried_then_reported(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("agent_core.illustrations.asyncio.sleep", _no_sleep)

    class _Throttled(_FakeImages):
        async def generate(self, **kwargs):
            self.calls.append(kwargs)
            raise _RateLimit("429")

    images = _Throttled()
    illustrator = IllustrationGenerator(type("C", (), {"images": images})(), _FakeWriter(), "m", max_attempts=2)  # type: ignore[arg-type]
    with pytest.raises(IllustrationError):
        await illustrator.generate("p", title="t")
    assert len(images.calls) == 2


def test_report_embeds_only_generated_illustrations():
    assert account_openai_base_url("https://acct.services.ai.azure.com/api/projects/proj/") == "https://acct.services.ai.azure.com/openai/v1"
    page = render_report_html("T", "body", images=[b"\x89PNG"])
    assert '<img src="data:image/png;base64,iVBORw==" alt="図解 1">' in page
    url = "https://acct.blob.core.windows.net/artifacts/images/2026/11/26/図解-ab12cd34.png?sv=x&sig=y"
    assert normalize_image_blob_name(url, "artifacts") == "images/2026/11/26/図解-ab12cd34.png"
    assert normalize_image_blob_name("artifacts/images/2026/11/26/a.png", "artifacts") == "images/2026/11/26/a.png"
    for bad in ("reports/2026/11/26/a.html", "images/2026/11/26/../../x.png", "images/a.png", "content/images/2026/11/26/a.png"):
        assert normalize_image_blob_name(bad, "artifacts") is None
