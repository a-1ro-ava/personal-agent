# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Entry point of the personal information-gathering Hosted Agent.

Microsoft Agent Framework agent served over the Foundry Responses protocol. Capabilities are
enabled from environment variables (see agent_core/config.py); missing settings simply disable
the related tools so the agent can be run and verified incrementally.
"""

from __future__ import annotations

import logging

from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient
from agent_framework_foundry_hosting import ResponsesHostServer
from azure.identity import DefaultAzureCredential
from azure.identity.aio import DefaultAzureCredential as AsyncDefaultAzureCredential
from dotenv import load_dotenv

from agent_core.activity_log import ActivityLoggingMiddleware
from agent_core.config import Settings
from agent_core.instructions import INSTRUCTIONS
from agent_core.knowledge import KnowledgeClient
from agent_core.reports import ReportWriter
from agent_core.store import Store
from agent_core.tools import Services, build_tools

load_dotenv(override=False)
logger = logging.getLogger("personal_agent")


def build_agent(settings: Settings) -> Agent:
    """Wire the chat client, tools and middleware into an Agent."""
    client = FoundryChatClient(
        project_endpoint=settings.project_endpoint,
        model=settings.model,
        credential=DefaultAzureCredential(),
    )
    async_credential = AsyncDefaultAzureCredential()
    services = Services(
        settings=settings,
        knowledge=KnowledgeClient(settings, async_credential) if settings.knowledge_enabled else None,
        store=Store(settings, async_credential) if settings.store_enabled else None,
        reports=ReportWriter(settings, async_credential) if settings.reports_enabled else None,
    )
    tools = build_tools(services)
    if settings.web_search_enabled:
        tools.append(
            client.get_web_search_tool(
                user_location={"type": "approximate", "country": "JP", "city": "Tokyo"},
                search_context_size="low",
            )
        )
    logger.info(
        "capabilities: knowledge=%s topic_graph=%s store=%s reports=%s web_search=%s",
        settings.knowledge_enabled,
        settings.topic_graph_enabled,
        settings.store_enabled,
        settings.reports_enabled,
        settings.web_search_enabled,
    )
    return Agent(
        client=client,
        name="personal-agent",
        instructions=INSTRUCTIONS,
        tools=tools,
        middleware=[ActivityLoggingMiddleware(services.store)],
        # History is managed by the hosting infrastructure.
        default_options={"store": False},
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("azure").setLevel(logging.WARNING)
    ResponsesHostServer(build_agent(Settings.from_env())).run()


if __name__ == "__main__":
    main()
