# Copyright (c) 2026 a-1ro-ava
# SPDX-License-Identifier: MIT
"""Runtime settings for the personal agent, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Settings:
    """Environment-backed settings. Empty values disable the related capability."""

    project_endpoint: str
    model: str
    search_endpoint: str
    knowledge_base: str
    knowledge_source: str
    topic_knowledge_base: str
    topic_knowledge_source: str
    search_api_version: str
    search_preview_api_version: str
    cosmos_endpoint: str
    cosmos_database: str
    storage_account_url: str
    artifacts_container: str
    content_container: str
    report_link_hours: int
    web_search_enabled: bool
    web_search_model: str
    image_model: str

    @property
    def illustrations_enabled(self) -> bool:
        """Illustrations need an image deployment and Blob Storage to keep the generated PNGs."""
        return bool(self.image_model) and self.reports_enabled

    @property
    def web_research_enabled(self) -> bool:
        """Web search is delegated to a sub-agent when a dedicated deployment is configured."""
        return self.web_search_enabled and bool(self.web_search_model)

    @property
    def knowledge_enabled(self) -> bool:
        return bool(self.search_endpoint and self.knowledge_base and self.knowledge_source)

    @property
    def topic_graph_enabled(self) -> bool:
        return bool(self.search_endpoint and self.topic_knowledge_base and self.topic_knowledge_source)

    @property
    def store_enabled(self) -> bool:
        return bool(self.cosmos_endpoint)

    @property
    def reports_enabled(self) -> bool:
        return bool(self.storage_account_url and self.artifacts_container)

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            project_endpoint=_env("FOUNDRY_PROJECT_ENDPOINT") or _env("AZURE_AI_PROJECT_ENDPOINT"),
            model=_env("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-6.1-sol"),
            search_endpoint=_env("SEARCH_ENDPOINT").rstrip("/"),
            knowledge_base=_env("KNOWLEDGE_BASE_NAME"),
            knowledge_source=_env("KNOWLEDGE_SOURCE_NAME"),
            topic_knowledge_base=_env("TOPIC_KNOWLEDGE_BASE_NAME"),
            topic_knowledge_source=_env("TOPIC_KNOWLEDGE_SOURCE_NAME"),
            search_api_version=_env("SEARCH_API_VERSION", "2026-10-01"),
            search_preview_api_version=_env("SEARCH_PREVIEW_API_VERSION", "2026-11-01-preview"),
            cosmos_endpoint=_env("COSMOS_ENDPOINT"),
            cosmos_database=_env("COSMOS_DATABASE", "personal-agent"),
            storage_account_url=_env("STORAGE_ACCOUNT_URL"),
            artifacts_container=_env("ARTIFACTS_CONTAINER", "artifacts"),
            content_container=_env("CONTENT_CONTAINER", "content"),
            report_link_hours=int(_env("REPORT_LINK_HOURS", "24") or 0),
            web_search_enabled=_env("WEB_SEARCH_ENABLED", "true").lower() not in ("0", "false", "no"),
            web_search_model=_env("WEB_SEARCH_MODEL_DEPLOYMENT_NAME"),
            image_model=_env("IMAGE_MODEL_DEPLOYMENT_NAME"),
        )
