# Copyright (c) 2026 a-1ro-ava
# SPDX-License-Identifier: MIT
"""HTML report generation (Markdown -> sanitized HTML) and artifact uploads to Blob Storage."""

from __future__ import annotations

import base64
import html
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import nh3
from azure.core.credentials_async import AsyncTokenCredential
from azure.storage.blob import BlobSasPermissions, ContentSettings, generate_blob_sas
from azure.storage.blob.aio import BlobServiceClient
from markdown_it import MarkdownIt

from .config import Settings

_MD = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table").enable("strikethrough")

ALLOWED_TAGS = {
    "a", "abbr", "b", "blockquote", "br", "code", "del", "em", "h1", "h2", "h3", "h4", "h5", "h6",
    "hr", "i", "li", "ol", "p", "pre", "s", "strong", "sub", "sup", "table", "tbody", "td", "th",
    "thead", "tr", "ul",
}  # fmt: skip
ALLOWED_ATTRIBUTES = {"a": {"href", "title"}, "th": {"align"}, "td": {"align"}, "code": {"class"}}

# No scripts, no remote resources: the report is viewed directly from Blob Storage.
CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'"

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="{csp}">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>{title}</title>
<style>
body {{ font-family: "Segoe UI", "Hiragino Sans", "Noto Sans JP", sans-serif; line-height: 1.7; max-width: 920px; margin: 2rem auto; padding: 0 1rem; color: #1f2328; }}
h1 {{ border-bottom: 2px solid #d0d7de; padding-bottom: .3rem; }}
h2 {{ border-bottom: 1px solid #d0d7de; padding-bottom: .2rem; margin-top: 2rem; }}
table {{ border-collapse: collapse; width: 100%; }} th, td {{ border: 1px solid #d0d7de; padding: .4rem .6rem; }}
th {{ background: #f6f8fa; }} code, pre {{ background: #f6f8fa; border-radius: 4px; }} pre {{ padding: .8rem; overflow-x: auto; }}
blockquote {{ border-left: 4px solid #d0d7de; margin: 0; padding: 0 1rem; color: #59636e; }}
.meta {{ color: #59636e; font-size: .9rem; }}
figure {{ margin: 1.5rem 0; text-align: center; }} figure img {{ max-width: 100%; height: auto; border: 1px solid #d0d7de; border-radius: 6px; }}
</style>
</head>
<body>
<h1>{title}</h1>
<p class="meta">生成日時: {generated} / Personal Agent</p>
{figures}{body}
</body>
</html>
"""

MAX_REPORT_IMAGES = 4
# Only illustrations written by ReportWriter.save_image can be embedded in a report (PNG from earlier versions).
_IMAGE_BLOB_RE = re.compile(r"^images/\d{4}/\d{2}/\d{2}/[0-9A-Za-z\u3040-\u30ff\u4e00-\u9fff-]+\.(jpg|png)$")


def normalize_image_blob_name(name: str, container: str) -> str | None:
    """Return the blob path of a generated illustration inside ``container``, or None if it is not one."""
    path = name.strip().split("?", 1)[0].lstrip("/")
    marker = f"/{container}/"
    if "://" in path and marker in path:
        path = path.split(marker, 1)[1]
    elif path.startswith(f"{container}/"):
        path = path[len(container) + 1 :]
    return path if _IMAGE_BLOB_RE.match(path) else None


def _image_media_type(data: bytes) -> str:
    return "image/png" if data.startswith(b"\x89PNG") else "image/jpeg"


def _render_figures(images: list[bytes]) -> str:
    return "".join(
        f'<figure><img src="data:{_image_media_type(data)};base64,{base64.b64encode(data).decode("ascii")}" alt="図解 {i}"></figure>\n'
        for i, data in enumerate(images, start=1)
    )


def render_report_html(title: str, markdown: str, generated_at: datetime | None = None, images: list[bytes] | None = None) -> str:
    """Render Markdown into a self-contained, sanitized HTML page with optional embedded illustrations."""
    raw_html = _MD.render(markdown)
    safe_body = nh3.clean(
        raw_html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        url_schemes={"http", "https", "mailto"},
        link_rel="noopener noreferrer nofollow",
    )
    generated = (generated_at or datetime.now(UTC)).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    return PAGE_TEMPLATE.format(
        csp=CSP,
        title=html.escape(title.strip() or "Report"),
        generated=generated,
        figures=_render_figures(images or []),
        body=safe_body,
    )


def _slug(title: str, default: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u3040-\u30ff\u4e00-\u9fff]+", "-", title).strip("-")[:60] or default


def report_blob_name(title: str, now: datetime | None = None) -> str:
    """Blob path: reports/YYYY/MM/DD/<slug>-<short id>.html"""
    now = now or datetime.now(UTC)
    return f"reports/{now:%Y/%m/%d}/{_slug(title, 'report')}-{uuid.uuid4().hex[:8]}.html"


def image_blob_name(title: str, now: datetime | None = None) -> str:
    """Blob path: images/YYYY/MM/DD/<slug>-<short id>.jpg"""
    now = now or datetime.now(UTC)
    return f"images/{now:%Y/%m/%d}/{_slug(title, 'illustration')}-{uuid.uuid4().hex[:8]}.jpg"


class ReportWriter:
    """Uploads reports and illustrations to the artifacts container."""

    def __init__(self, settings: Settings, credential: AsyncTokenCredential) -> None:
        self._settings = settings
        self._service = BlobServiceClient(settings.storage_account_url, credential=credential)

    async def close(self) -> None:
        await self._service.close()

    async def save(self, title: str, markdown: str, image_blobs: list[str] | None = None) -> dict[str, Any]:
        images: list[bytes] = []
        skipped: list[str] = []
        for ref in (image_blobs or [])[:MAX_REPORT_IMAGES]:
            path = normalize_image_blob_name(ref, self._settings.artifacts_container)
            if path is None:
                skipped.append(ref)
                continue
            try:
                images.append(await self._download(path))
            except Exception:  # noqa: BLE001 - a missing illustration must not block the report
                skipped.append(ref)
        result = await self._upload(
            report_blob_name(title), render_report_html(title, markdown, images=images).encode("utf-8"), "text/html; charset=utf-8", "report"
        )
        if image_blobs:
            result["embeddedImages"] = len(images)
            if skipped:
                result["skippedImages"] = skipped
        return result

    async def save_image(self, title: str, jpeg: bytes) -> dict[str, Any]:
        return await self._upload(image_blob_name(title), jpeg, "image/jpeg", "illustration")

    async def _download(self, blob_name: str) -> bytes:
        blob = self._service.get_blob_client(self._settings.artifacts_container, blob_name)
        return await (await blob.download_blob()).readall()

    async def _upload(self, name: str, data: bytes, content_type: str, kind: str) -> dict[str, Any]:
        blob = self._service.get_blob_client(self._settings.artifacts_container, name)
        await blob.upload_blob(data, overwrite=True, content_settings=ContentSettings(content_type=content_type), metadata={"kind": kind})
        result: dict[str, Any] = {"blob": f"{self._settings.artifacts_container}/{name}", "url": blob.url}
        hours = self._settings.report_link_hours
        if hours > 0:
            try:
                start = datetime.now(UTC) - timedelta(minutes=5)
                expiry = start + timedelta(hours=hours)
                key = await self._service.get_user_delegation_key(start, expiry)
                sas = generate_blob_sas(
                    account_name=blob.account_name,
                    container_name=self._settings.artifacts_container,
                    blob_name=name,
                    user_delegation_key=key,
                    permission=BlobSasPermissions(read=True),
                    start=start,
                    expiry=expiry,
                )
                result["viewUrl"] = f"{blob.url}?{sas}"
                result["viewUrlExpiresAt"] = expiry.isoformat(timespec="minutes")
            except Exception as e:  # noqa: BLE001 - the report is saved even if a link cannot be issued
                result["viewUrlError"] = f"{type(e).__name__}: {e}"[:300]
        return result

    async def read_collected(self, container: str, blob_name: str, max_chars: int = 20_000) -> str:
        data = await (await self._service.get_blob_client(container, blob_name).download_blob()).readall()
        text = data.decode("utf-8", errors="replace")
        return text if len(text) <= max_chars else text[:max_chars] + "\n…(truncated)"
