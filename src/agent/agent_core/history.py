# Copyright (c) 2026 a-1ro-ava
# SPDX-License-Identifier: MIT
"""Repairs conversation history replayed by the hosting infrastructure.

The Responses host replays only the latest ``DEFAULT_FETCH_HISTORY_ITEM_COUNT`` (default 100) items
of a conversation. In long conversations (e.g. Teams chats) the cut can fall between a
``function_call`` and its ``function_call_output``; the orphaned output makes the model reject every
later turn with "No tool call found for function call output with call_id ...".
"""

from __future__ import annotations

import logging

from agent_framework import AgentContext, AgentMiddleware, Message

logger = logging.getLogger(__name__)


def drop_unpaired_tool_calls(messages: list[Message]) -> list[Message]:
    """Remove function calls without results and results without calls, dropping emptied messages."""
    call_ids = {c.call_id for m in messages for c in m.contents if c.type == "function_call" and c.call_id}
    result_ids = {c.call_id for m in messages for c in m.contents if c.type == "function_result" and c.call_id}
    unpaired = call_ids ^ result_ids
    if not unpaired:
        return messages

    repaired: list[Message] = []
    for message in messages:
        contents = [
            c for c in message.contents if not (c.type in ("function_call", "function_result") and c.call_id in unpaired)
        ]
        if len(contents) == len(message.contents):
            repaired.append(message)
        elif contents:
            message.contents = contents
            repaired.append(message)
    logger.warning("Dropped %d unpaired tool call item(s) from the conversation history", len(unpaired))
    return repaired


class HistoryRepairMiddleware(AgentMiddleware):
    """Drops tool calls/results whose counterpart fell outside the replayed history window."""

    async def process(self, context: AgentContext, call_next) -> None:  # type: ignore[override]
        context.messages[:] = drop_unpaired_tool_calls(context.messages)
        await call_next()
