# Copyright (c) 2026 a-1ro-ava
# SPDX-License-Identifier: MIT
"""Agent middleware: records conversation turns and web search activity in Cosmos DB."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from agent_framework import AgentContext, AgentMiddleware, AgentResponse, Content, Message

from .instructions import current_time_note
from .store import Store

logger = logging.getLogger(__name__)

LAST_USER_TEXT_KEY = "pa_last_user_text"
SESSION_ID_KEY = "pa_session_id"


def last_user_text(messages: list[Message]) -> str:
    """Return the text of the most recent user message."""
    for message in reversed(messages):
        if str(message.role) == "user":
            return message.text or ""
    return ""


def extract_activity(response: AgentResponse) -> dict[str, Any]:
    """Collect tool calls, web search queries and cited URLs from an agent response."""
    tool_calls: list[dict[str, Any]] = []
    queries: list[str] = []
    urls: dict[str, dict[str, str]] = {}
    for message in response.messages:
        for content in message.contents:
            ctype = content.type
            if ctype == "function_call":
                tool_calls.append({"name": content.name, "arguments": _short(content.arguments)})
            elif ctype in ("search_tool_call", "search_tool_result"):
                payload = content.arguments if ctype == "search_tool_call" else (content.result or {}).get("action")
                action = _as_dict(payload)
                if action.get("query") and action["query"] not in queries:
                    queries.append(str(action["query"]))
                for q in action.get("queries") or []:
                    if q not in queries:
                        queries.append(str(q))
                for src in action.get("sources") or []:
                    if isinstance(src, dict) and src.get("url"):
                        urls.setdefault(src["url"], {"url": src["url"], "title": src.get("title", "")})
                if ctype == "search_tool_call":
                    tool_calls.append({"name": content.tool_name or "web_search", "arguments": _short(action)})
            for annotation in content.annotations or []:
                url = annotation.get("url")
                if url:
                    urls.setdefault(url, {"url": url, "title": annotation.get("title", "")})
    return {"toolCalls": tool_calls, "queries": queries, "urls": list(urls.values())}


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _short(value: Any, limit: int = 600) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text[:limit]


class ActivityLoggingMiddleware(AgentMiddleware):
    """Stores each turn and any web searches. Logging failures never break the conversation."""

    def __init__(self, store: Store | None) -> None:
        self._store = store
        self._pending: set[asyncio.Task[None]] = set()

    async def process(self, context: AgentContext, call_next) -> None:  # type: ignore[override]
        context.messages.insert(0, Message(role="system", contents=[Content.from_text(current_time_note())]))
        user_text = last_user_text(context.messages)
        session_id = (context.session.session_id if context.session else None) or str(uuid.uuid4())
        if context.session is not None:
            # Read by tools through FunctionInvocationContext.session.state.
            context.session.state[LAST_USER_TEXT_KEY] = user_text
            context.session.state[SESSION_ID_KEY] = session_id

        if context.stream:
            async def _on_result(response: AgentResponse) -> AgentResponse:
                self._schedule(session_id, user_text, response)
                return response

            context.stream_result_hooks.append(_on_result)
            await call_next()
        else:
            await call_next()
            if isinstance(context.result, AgentResponse):
                self._schedule(session_id, user_text, context.result)

    def _schedule(self, session_id: str, user_text: str, response: AgentResponse) -> None:
        if self._store is None:
            return
        task = asyncio.create_task(self._write(session_id, user_text, response))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _write(self, session_id: str, user_text: str, response: AgentResponse) -> None:
        assert self._store is not None
        try:
            activity = extract_activity(response)
            await self._store.add_conversation_turn(session_id, user_text, response.text or "", activity["toolCalls"])
            if activity["queries"] or activity["urls"]:
                await self._store.add_web_search_log(session_id, user_text, activity["queries"], activity["urls"])
        except Exception:  # noqa: BLE001
            logger.exception("Failed to write activity log")
