# Copyright (c) 2026 a-1ro-ava
# SPDX-License-Identifier: MIT
"""Illustrations of research results generated with a gpt-image deployment.

Image generation is slow and costly, so it only runs when the user's latest message explicitly asks
for a picture (「絵にして」, 「イラストで」, 「図解して」, ...). The check is done in code on the user's
own words, not left to the model's judgement.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import random
import re
from typing import Any, Literal

from azure.core.credentials_async import AsyncTokenCredential
from azure.identity.aio import get_bearer_token_provider
from openai import AsyncOpenAI

from .reports import ReportWriter
from .web_research import is_rate_limited, retry_after_seconds

logger = logging.getLogger(__name__)

ImageSize = Literal["1536x1024", "1024x1024", "1024x1536"]
ImageQuality = Literal["low", "medium", "high"]

MAX_PROMPT_CHARS = 3000
MAX_ILLUSTRATIONS_PER_TURN = 2

STYLE_GUIDE = """\
調査結果を1枚で伝える、清潔感のあるフラットデザインの図解イラスト。白基調の背景、論点ごとに区切ったレイアウト、アイコンと矢印で関係を示す。
文字は短い見出しやキーワード程度に留め、指定された語句を正確に表記する。実在の人物の顔、企業ロゴ、透かしは描かない。
"""

_REQUEST_PATTERNS = [
    # 絵にして / 絵で説明して / 絵を描いて / 絵にまとめて / 絵化して
    r"絵(?!文字|画|本)(にして|にでき|にした[いら]|にまとめ|に描|に起こ|化|で(まとめ|説明|表|示|見せ|教え|描|お願い|ほし|欲し|伝え)|を(描|かい|書い|生成|作|つく|見せ))",
    r"イラスト(にして|化|で(まとめ|説明|表|示|見せ|教え|お願い|ほし|欲し|伝え)|にまとめ|を(描|かい|書い|生成|作|つく))",
    r"図解(して|化|で(まとめ|説明|表|示|お願い|ほし|欲し|伝え)|を(作|つく|生成|お願い)|にして|にまとめ|お願い)",
    r"図にして|図にまとめ",
    r"画像(にして|化し|にまとめ|で(まとめ|説明|表|示|見せ|伝え)|を(作|つく|生成し|生成で))",
    r"描いて(?!い|る)|描けますか",
    r"\b(draw|illustrate|visuali[sz]e)\b",
    r"\b(make|generate|create|turn)\b.{0,40}\b(picture|image|illustration|infographic|drawing)",
    r"\bas an? (picture|image|illustration|infographic|drawing)\b",
]
_REQUEST_RE = re.compile("|".join(f"(?:{p})" for p in _REQUEST_PATTERNS), re.IGNORECASE)


def illustration_requested(user_text: str) -> bool:
    """True when the user's message explicitly asks for a picture of the content."""
    return bool(_REQUEST_RE.search(" ".join(user_text.split())))


def account_openai_base_url(project_endpoint: str) -> str:
    """Account-level OpenAI v1 URL. The project-scoped ``/openai/v1`` route does not serve image generation."""
    return project_endpoint.rstrip("/").split("/api/projects/", 1)[0] + "/openai/v1"


def create_image_client(project_endpoint: str, credential: AsyncTokenCredential) -> AsyncOpenAI:
    return AsyncOpenAI(
        base_url=account_openai_base_url(project_endpoint),
        api_key=get_bearer_token_provider(credential, "https://cognitiveservices.azure.com/.default"),
    )


class IllustrationError(RuntimeError):
    """Raised when an illustration cannot be generated."""


class IllustrationGenerator:
    """Generates PNG illustrations with a gpt-image deployment and stores them in the artifacts container."""

    def __init__(
        self,
        client: Any,
        writer: ReportWriter,
        model: str,
        *,
        max_attempts: int = 3,
        max_backoff_seconds: float = 30.0,
        timeout_seconds: float = 240.0,
    ) -> None:
        # ``client`` is an AsyncOpenAI client for the account endpoint (see create_image_client).
        self._client = client
        self._writer = writer
        self._model = model
        self._max_attempts = max_attempts
        self._max_backoff = max_backoff_seconds
        self._timeout = timeout_seconds
        # The image deployment has a very small quota; serialize requests.
        self._semaphore = asyncio.Semaphore(1)

    async def generate(self, prompt: str, *, title: str, size: ImageSize = "1536x1024", quality: ImageQuality = "medium") -> dict[str, Any]:
        """Generate one illustration and return the saved blob path and view URL."""
        body = prompt.strip()[:MAX_PROMPT_CHARS]
        if not body:
            raise IllustrationError("prompt is empty")
        async with self._semaphore:
            png = await self._generate_with_retry(f"{STYLE_GUIDE}\n{body}", size, quality)
        saved = await self._writer.save_image(title, png)
        return {**saved, "model": self._model, "size": size, "quality": quality}

    async def _generate_with_retry(self, prompt: str, size: str, quality: str) -> bytes:
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await self._client.images.generate(
                    model=self._model, prompt=prompt, size=size, quality=quality, n=1, timeout=self._timeout
                )
                data = response.data[0].b64_json if response.data else None
                if not data:
                    raise IllustrationError("the image model returned no image")
                return base64.b64decode(data)
            except IllustrationError:
                raise
            except Exception as e:  # noqa: BLE001
                if not is_rate_limited(e) or attempt == self._max_attempts:
                    raise IllustrationError(f"{type(e).__name__}: {e}"[:300]) from e
                delay = retry_after_seconds(e) or (5.0 * 2 ** (attempt - 1))
                delay = min(self._max_backoff, delay) + random.uniform(0, 1)
                logger.warning("image generation rate limited (attempt %d/%d); retrying in %.1fs", attempt, self._max_attempts, delay)
                await asyncio.sleep(delay)
        raise IllustrationError("unreachable")
