# Copyright (c) 2026 a-1ro-ava
# SPDX-License-Identifier: MIT
"""Function tools exposed to the model."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from agent_framework import FunctionInvocationContext, tool
from pydantic import Field

from pa_shared.sources import SOURCE_KINDS, InvalidSourceError

from .activity_log import ILLUSTRATION_COUNT_KEY, LAST_USER_TEXT_KEY, SESSION_ID_KEY
from .config import Settings
from .illustrations import (
    MAX_ILLUSTRATIONS_PER_TURN,
    IllustrationError,
    IllustrationGenerator,
    ImageQuality,
    ImageSize,
    illustration_requested,
)
from .knowledge import KnowledgeClient, KnowledgeError
from .reports import MAX_REPORT_IMAGES, ReportWriter
from .store import Store
from .web_research import WebResearcher, WebResearchError

logger = logging.getLogger(__name__)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _state(ctx: FunctionInvocationContext | None, key: str) -> str:
    if ctx is None or ctx.session is None:
        return ""
    return str(ctx.session.state.get(key) or "")


def confirmation_matches(confirmation: str, last_user_text: str) -> bool:
    """True when the quoted confirmation appears in the user's latest message.

    Guards the approval rule: a collection target is only registered when the user's own
    words (not the model's) approve it in the current turn.
    """
    quoted = "".join(confirmation.split())
    said = "".join(last_user_text.split())
    return bool(quoted) and quoted in said


@dataclass
class Services:
    settings: Settings
    knowledge: KnowledgeClient | None
    store: Store | None
    reports: ReportWriter | None
    researcher: WebResearcher | None = None
    illustrator: IllustrationGenerator | None = None


def build_tools(svc: Services) -> list[Any]:
    """Create the tool functions bound to the given services."""
    tools: list[Any] = []

    if svc.researcher is not None:
        researcher = svc.researcher
        log_store = svc.store

        @tool(approval_mode="never_require")
        async def web_research(
            ctx: FunctionInvocationContext,
            query: Annotated[str, Field(description="調べたい内容。期間・対象などの条件も含めた具体的な依頼文。")],
            focus: Annotated[str, Field(description="重視する観点や除外条件 (例: 「本日公開分のみ」「トレンド上位」)")] = "",
            domains: Annotated[
                list[str], Field(description="検索対象を限定するドメイン (例: [\"qiita.com\", \"zenn.dev\"])。不要なら空。")
            ] = [],  # noqa: B006
            max_results: Annotated[int, Field(description="要約に含める記事の最大件数 (1-15)")] = 8,
        ) -> str:
            """Web検索専用のサブエージェントで検索し、出典URL付きの要約を返す。蓄積情報にない最新情報・未収集情報の補完に使う。"""
            try:
                result = await researcher.research(query, focus=focus, domains=domains, max_results=max_results)
            except WebResearchError as e:
                return _json({"error": str(e), "hint": "Web検索が一時的に利用できません。蓄積情報で回答するか、時間をおいて再試行してください。"})
            if log_store is not None and (result["queries"] or result["sources"]):
                try:
                    await log_store.add_web_search_log(
                        _state(ctx, SESSION_ID_KEY), _state(ctx, LAST_USER_TEXT_KEY), result["queries"], result["sources"]
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("Failed to write web search log")
            return _json(result)

        tools.append(web_research)

    if svc.knowledge is not None:
        knowledge = svc.knowledge

        @tool(approval_mode="never_require")
        async def search_knowledge(
            query: Annotated[str, Field(description="具体的な検索質問。日本語・英語どちらでも可。")],
            effort: Annotated[
                Literal["minimal", "low", "medium"],
                Field(description="minimal=キーワード的な事実確認, low=通常, medium=複雑な質問(クエリ分解)"),
            ] = "low",
            max_documents: Annotated[int, Field(description="返す根拠の最大件数 (1-50)")] = 10,
        ) -> str:
            """蓄積済みの情報資産 (Foundry IQ) を通常検索し、詳細な根拠を取得する。事実確認や特定論点の深掘りに使う。"""
            try:
                return await knowledge.search(query, effort=effort, max_documents=max_documents)
            except KnowledgeError as e:
                return _json({"error": str(e)})

        @tool(approval_mode="never_require")
        async def explore_topics(
            query: Annotated[str, Field(description="広く関連情報を集めたいテーマや問い。")],
            mode: Annotated[
                Literal["diverse", "balanced"],
                Field(description="diverse=多様な観点を優先, balanced=関連度と多様性のバランス"),
            ] = "diverse",
            diversity: Annotated[float, Field(description="diverse時の多様性係数 (0-1)")] = 0.3,
            max_documents: Annotated[int, Field(description="返す根拠の最大件数 (1-50)")] = 20,
        ) -> str:
            """トピックグラフで関連テーマと幅広い根拠を取得する。深掘りの最初の一手、トレンドや全体像の把握に使う。"""
            try:
                return await knowledge.explore(query, topic_mode=mode, diversity=diversity, max_documents=max_documents)
            except KnowledgeError as e:
                return _json({"error": str(e)})

        tools += [search_knowledge, explore_topics]

    if svc.reports is not None:
        reports = svc.reports
        content_container = svc.settings.content_container

        @tool(approval_mode="never_require")
        async def read_collected_document(
            blob_name: Annotated[str, Field(description="content コンテナ内のパス (例: rss-xxxx/2026/11/18/abcd.md)")],
        ) -> str:
            """収集済みドキュメント (正規化済み Markdown) の全文を読む。検索結果の断片だけでは不足するときに使う。"""
            name = blob_name.strip().lstrip("/")
            if name.startswith(f"{content_container}/"):
                name = name[len(content_container) + 1 :]
            if not name or ".." in name.split("/"):
                return _json({"error": "invalid blob name"})
            try:
                return await reports.read_collected(content_container, name)
            except Exception as e:  # noqa: BLE001
                return _json({"error": f"{type(e).__name__}: {e}"[:300]})

        @tool(approval_mode="never_require")
        async def save_html_report(
            title: Annotated[str, Field(description="レポートのタイトル")],
            markdown: Annotated[str, Field(description="レポート本文 (Markdown)。出典URLをリンクで含めること。")],
            image_blobs: Annotated[
                list[str],
                Field(description=f"埋め込む図解 (generate_illustration が返した blob) の一覧。最大{MAX_REPORT_IMAGES}件。不要なら空。"),
            ] = [],  # noqa: B006
        ) -> str:
            """調査結果をHTMLレポートとして生成しBlobに保存する。閲覧用URLを返す。生成済みの図解を埋め込める。"""
            try:
                return _json(await reports.save(title, markdown, image_blobs))
            except Exception as e:  # noqa: BLE001
                return _json({"error": f"{type(e).__name__}: {e}"[:300]})

        tools += [read_collected_document, save_html_report]

    if svc.illustrator is not None:
        illustrator = svc.illustrator

        @tool(approval_mode="never_require")
        async def generate_illustration(
            ctx: FunctionInvocationContext,
            prompt: Annotated[
                str,
                Field(
                    description="描く内容。調査結果の要点・論点の関係・構図を具体的に書く。図中に入れる短い見出し語句は「」で示す。"
                ),
            ],
            title: Annotated[str, Field(description="図解のタイトル (保存名に使う)")],
            size: Annotated[ImageSize, Field(description="1536x1024=横長 (既定), 1024x1024=正方形, 1024x1536=縦長")] = "1536x1024",
            quality: Annotated[ImageQuality, Field(description="low/medium/high。通常は medium")] = "medium",
        ) -> str:
            """調査内容を図解イラスト (PNG) にして保存し、閲覧URLを返す。ユーザーが今回のメッセージで絵・イラスト・図解を明示的に求めたときだけ使う。"""
            if not illustration_requested(_state(ctx, LAST_USER_TEXT_KEY)):
                return _json(
                    {
                        "result": "not_requested",
                        "message": "ユーザーの最新メッセージに絵・イラスト・図解の明示的な依頼がないため生成しません。文章で回答してください。",
                    }
                )
            count = int(ctx.session.state.get(ILLUSTRATION_COUNT_KEY) or 0) if ctx.session else 0
            if count >= MAX_ILLUSTRATIONS_PER_TURN:
                return _json({"result": "limit_reached", "message": f"1回の依頼で生成できる図解は{MAX_ILLUSTRATIONS_PER_TURN}枚までです。"})
            if ctx.session is not None:
                ctx.session.state[ILLUSTRATION_COUNT_KEY] = count + 1
            try:
                return _json({"result": "generated", **await illustrator.generate(prompt, title=title, size=size, quality=quality)})
            except IllustrationError as e:
                return _json({"error": str(e), "hint": "図解を生成できませんでした。文章での回答を続けてください。"})

        tools.append(generate_illustration)

    if svc.store is not None:
        store = svc.store

        @tool(approval_mode="never_require")
        async def list_recent_collected_items(
            days: Annotated[int, Field(description="何日前までに収集したものか (1-30)")] = 3,
            limit: Annotated[int, Field(description="最大件数 (1-100)")] = 30,
        ) -> str:
            """定期収集で最近取り込んだ記事・論文・リリースの一覧を返す。「今日の新着」などに使う。"""
            return _json(await store.list_recent_items(max(1, min(days, 30)), max(1, min(limit, 100))))

        @tool(approval_mode="never_require")
        async def list_collection_sources(
            include_disabled: Annotated[bool, Field(description="無効化した対象も含めるか")] = True,
        ) -> str:
            """現在の情報収集対象一覧 (RSS, Webページ, GitHubリリース, arXiv) を返す。"""
            return _json(await store.list_sources(include_disabled))

        @tool(approval_mode="never_require")
        async def set_collection_source_enabled(
            source_id: Annotated[str, Field(description="収集対象のID")],
            enabled: Annotated[bool, Field(description="true=有効化, false=停止")],
        ) -> str:
            """ユーザーの明示的な指示に基づき、既存の収集対象を停止・再開する。"""
            doc = await store.set_source_enabled(source_id, enabled)
            return _json({"result": "updated", "id": source_id, "enabled": enabled} if doc else {"result": "not_found"})

        @tool(approval_mode="never_require")
        async def review_history_for_candidates(
            days: Annotated[int, Field(description="分析対象の期間 (日数, 1-60)")] = 14,
        ) -> str:
            """会話ログ・Web検索ログ・既存の収集対象を取得する。新しい情報収集対象の候補を考えるために使う。"""
            history = await store.recent_history(max(1, min(days, 60)))
            history["currentSources"] = [
                {"id": s["id"], "kind": s["kind"], "target": s["target"], "name": s["name"]} for s in await store.list_sources(True)
            ]
            history["pendingCandidates"] = await store.list_candidates("proposed")
            return _json(history)

        @tool(approval_mode="never_require")
        async def propose_source_candidate(
            ctx: FunctionInvocationContext,
            kind: Annotated[str, Field(description=f"情報源の種類: {', '.join(SOURCE_KINDS)}")],
            target: Annotated[
                str,
                Field(description="rss/web=URL, github_releases=owner/repo, arxiv=arXiv検索クエリ (例: cat:cs.CL AND abs:agent)"),
            ],
            name: Annotated[str, Field(description="表示名")],
            reason: Annotated[str, Field(description="継続収集すると有益だと考えた理由")],
            evidence: Annotated[list[str], Field(description="根拠となった会話・検索・URL (最大10件)")] = [],  # noqa: B006
        ) -> str:
            """新しい情報収集対象を「候補」として保存する。収集対象一覧には登録しない (登録はユーザー承認後)。"""
            try:
                result = await store.propose_candidate(kind, target, name, reason, evidence, _state(ctx, SESSION_ID_KEY) or None)
            except InvalidSourceError as e:
                return _json({"result": "invalid", "error": str(e)})
            return _json(result)

        @tool(approval_mode="never_require")
        async def list_source_candidates(
            status: Annotated[
                Literal["proposed", "approved", "rejected", "all"], Field(description="絞り込むステータス")
            ] = "proposed",
        ) -> str:
            """情報収集対象の候補と承認・却下状態を一覧する。"""
            return _json(await store.list_candidates(None if status == "all" else status))

        @tool(approval_mode="never_require")
        async def approve_source_candidate(
            ctx: FunctionInvocationContext,
            candidate_id: Annotated[str, Field(description="承認する候補ID (cand-...)")],
            user_confirmation: Annotated[
                str, Field(description="ユーザーが今回のメッセージで承認した発言をそのまま引用 (例: 「候補1を承認します」)")
            ],
            tags: Annotated[list[str], Field(description="付与するタグ")] = [],  # noqa: B006
            max_items: Annotated[int, Field(description="1回の収集で取り込む最大件数 (1-100)")] = 20,
        ) -> str:
            """ユーザーが明示的に承認した候補だけを収集対象一覧に登録する。"""
            if not confirmation_matches(user_confirmation, _state(ctx, LAST_USER_TEXT_KEY)):
                return _json(
                    {
                        "result": "confirmation_required",
                        "message": "ユーザーの最新メッセージに承認の発言が見つかりません。候補IDを示して明示的な承認を求めてください。",
                    }
                )
            return _json(await store.approve_candidate(candidate_id, user_confirmation, tags, max_items))

        @tool(approval_mode="never_require")
        async def reject_source_candidate(
            candidate_id: Annotated[str, Field(description="却下する候補ID (cand-...)")],
            reason: Annotated[str, Field(description="却下理由 (ユーザーの発言の要約)")] = "",
        ) -> str:
            """ユーザーが却下した候補を却下済みにする。却下済みの候補は90日間は再提案されない。"""
            return _json(await store.reject_candidate(candidate_id, reason))

        tools += [
            list_recent_collected_items,
            list_collection_sources,
            set_collection_source_enabled,
            review_history_for_candidates,
            propose_source_candidate,
            list_source_candidates,
            approve_source_candidate,
            reject_source_candidate,
        ]

    return tools
