---
title: パーソナル情報収集エージェント
description: Microsoft Agent Framework で実装し Foundry Hosted Agent として動かす個人向け情報収集・調査エージェント
ms.date: 2026-11-18
ms.topic: overview
---

## 概要

日々の情報収集・蓄積・検索・深掘りを支援する個人向けエージェントです。要件は [docs/requires.md](docs/requires.md) を参照してください。

| 構成要素                                                       | 実装                                                                 | リージョン |
|----------------------------------------------------------------|----------------------------------------------------------------------|------------|
| エージェント                                                   | Microsoft Agent Framework (Python) / Foundry Hosted Agent            | East US 2  |
| モデル                                                         | `gpt-6.1-sol` (応答)、`gpt-5.4-mini` (検索計画)、`gpt-5.4-mini-search` (Web検索サブエージェント)、`text-embedding-3-large` | East US 2  |
| 情報検索基盤                                                   | Foundry IQ (サーバーレス Azure AI Search) のナレッジベース            | Japan East |
| 原本・正規化コンテンツ・HTMLレポート                           | Azure Blob Storage (`raw` / `content` / `artifacts`)                 | Japan East |
| 収集対象・メタデータ・ログ・候補                               | Azure Cosmos DB (サーバーレス)                                       | Japan East |
| 定期収集                                                       | Azure Functions (Flex Consumption) 毎朝 6:00 JST                     | Japan East |

## 処理の流れ

```text
[Functions 06:00 JST] Cosmos DB sources ─▶ 収集 (RSS / Web / GitHub Releases / arXiv)
        │                                       │
        │                    raw/ (原本)  ◀─────┤
        │                    content/ (正規化 Markdown) ◀─┘
        └─▶ items / runs (Cosmos DB) ─▶ indexer 実行 ─▶ Foundry IQ (content-blob-ks / personal-kb)
                                                           └─ 同じインデックスを content-topic-ks / personal-topic-kb
                                                              がトピックインデックス付きで参照 (トピック再生成は手動)

[Hosted Agent] ユーザー ─▶ explore_topics (トピックグラフ) ─▶ search_knowledge (通常検索)
                        ─▶ web_research (検索専用モデルで Web 検索し要約) ─▶ 回答 (出典付き) ─▶ save_html_report (artifacts/)
                        ─▶ propose_source_candidate ─▶ ユーザー承認 ─▶ approve_source_candidate ─▶ sources
```

Web 検索は `web_research` ツールが専用デプロイ (`WEB_SEARCH_MODEL_DEPLOYMENT_NAME`) のサブエージェントで実行し、出典付きの要約だけを応答モデルへ返します。検索結果の大量のトークンが応答モデルのレート制限 (TPM) を消費しないようにするためです。429 は指数バックオフで再試行します。`WEB_SEARCH_MODEL_DEPLOYMENT_NAME` が空の場合は応答モデル自身が Web 検索します。

## リポジトリ構成

| パス                                                           | 内容                                                                                   |
|----------------------------------------------------------------|----------------------------------------------------------------------------------------|
| [azure.yaml](azure.yaml)                                       | azd プロジェクト (Foundry プロジェクト、モデル、Hosted Agent、フック)                  |
| [src/agent](src/agent)                                         | Hosted Agent (`main.py`、`agent_core/`)                                                |
| [src/functions](src/functions)                                 | 定期収集 Function App (`function_app.py`、`collector/`)                                |
| [src/shared/pa_shared](src/shared/pa_shared)                   | 収集対象の定義と正規化 (各アプリへ `scripts/sync_shared.py` でコピー)                  |
| [support-infra/main.bicep](support-infra/main.bicep)           | Foundry IQ、Blob、Cosmos DB、Functions、RBAC                                           |
| [scripts](scripts)                                             | azd フック、Foundry IQ 構成、収集対象の初期登録                                        |
| [config/sources.seed.json](config/sources.seed.json)           | 初期の収集対象                                                                         |
| [docs/topic-graph.md](docs/topic-graph.md)                     | トピックグラフ (プレビュー) の構成・再生成手順と検証結果                               |

> [!NOTE]
> `infra/` 配下に Bicep を置くと `microsoft.foundry` プロバイダーがそれをプロビジョニングテンプレートとして扱うため、補助リソースは `support-infra/` に置いています。

## デプロイ

前提: `az login` と `azd auth login` 済み、`uv`、Azure Functions Core Tools、PowerShell 7。

```powershell
azd provision   # Foundry + モデル → postprovision フックで補助リソース、Foundry IQ、初期収集対象を構成
azd deploy      # Hosted Agent → postdeploy フックで Functions を発行し、エージェント ID にロールを付与
```

フックだけを再実行する場合は `azd hooks run postprovision` または `azd hooks run postdeploy` を使います。

## ローカル実行とテスト

```powershell
cd src/agent
uv sync
azd ai agent run --no-client            # http://localhost:8088
azd ai agent invoke --local "今日の新着を教えて"
```

VS Code では `F5` (Debug Local Agent/Workflow HTTP Server) で起動すると Agent Inspector が開きます。`src/agent/.env` は `.env.example` を元に `azd env get-values` の値で作成してください。

```powershell
cd src/agent; .\.venv\Scripts\python.exe -m pytest -q tests
cd src/functions; .\.venv\Scripts\python.exe -m pytest -q tests
```

## 運用

* 収集を手動で再実行するには `POST https://<function-app>.azurewebsites.net/api/collect` (関数キー必須、本文 `{"sourceIds": [...]}` は任意) を呼び出します
* 実行結果は Cosmos DB の `runs`、収集対象ごとの最終結果は `sources.lastRun` に記録されます
* 収集対象の追加は、エージェントとの会話で候補を提案させ、候補 ID を示して承認する方法を基本とします
* 初期登録を追加するには `config/sources.seed.json` を編集して `uv run scripts/seed_sources.py` を実行します
* HTML レポートは `artifacts` コンテナに保存され、24 時間有効な読み取り専用リンク (ユーザー委任 SAS) が返されます。`REPORT_LINK_HOURS=0` でリンク発行を無効化できます
* トピックグラフは日次収集では更新されません。チャンク数が大きく増えたら [docs/topic-graph.md](docs/topic-graph.md) の手順で新しい世代を生成します

## 設計上の判断

* 収集対象の登録は、ユーザーの最新メッセージに承認発言が含まれる場合だけ受け付けます (`approve_source_candidate` がサーバー側で照合)
* 却下した候補は 90 日間再提案しません。同じ対象の提案は決定的な候補 ID に集約されます
* RSS と arXiv の項目は初回取得の内容を保持し、Web ページと GitHub リリースは内容ハッシュで更新を検出します
* Web ページと記事本文の取得では robots.txt を確認し、5 MB を超える応答は取り込みません
* 外部コンテンツの指示に従わないことをエージェントの指示に明記し、生成 HTML はサニタイズと CSP でスクリプトを無効化しています
* 生成レポートは検索基盤に再投入していません (`content` コンテナのみをインデックス対象とする)
* ホストは会話履歴の直近 100 件 (`DEFAULT_FETCH_HISTORY_ITEM_COUNT`) だけを再生するため、Teams のような長い会話ではツール呼び出しと結果の組が境界で分断されます。`HistoryRepairMiddleware` が対になっていない呼び出しと結果を除いてからモデルに送ります

## 未対応・検証中の事項

* トピックグラフは構成済みですが、データ量が推奨値を下回るため品質効果は未評価です。再生成は承認カードを伴う手動操作です ([docs/topic-graph.md](docs/topic-graph.md))
* Voice Live との音声連携は未実装です (要件の検証ステップ 9)
* 収集元の削除 (ソース側で消えた記事) の反映は未実装です
