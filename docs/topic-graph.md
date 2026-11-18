---
title: トピックグラフの有効化手順
description: Foundry IQ のトピックグラフ (プレビュー) を通常検索とは独立した知識ベースとして有効化する手順と検証事項
ms.date: 2026-11-18
ms.topic: how-to
---

## 現在の構成

通常検索は `azureBlob` ナレッジソース `content-blob-ks` とナレッジベース `personal-kb` で動作しています。トピックグラフは `searchIndex` 種別のナレッジソースでのみ利用でき、事前に作成したトピックインデックスが必要です (2026-11 時点のプレビュー仕様)。

そのため、トピックグラフは別のナレッジソース `content-topic-ks` とナレッジベース `personal-topic-kb` として追加する構成にしています。どちらも `content-blob-ks` が自動生成したコンテンツインデックス `content-blob-ks-index` を参照します。未構成の間、エージェントの `explore_topics` は通常検索 (`effort=medium`、最大 20 件) で代替し、結果に `mode: explore-fallback` を付けます。

## 有効化の手順

1. GitHub Copilot CLI に `foundry-iq-skills` プラグインを追加し、コンテンツインデックス `content-blob-ks-index` からトピックインデックスを作成します。

   ```text
   /plugin marketplace add microsoft/azure-skills
   /plugin install foundry-iq-skills@azure-skills
   ```

2. 作成したトピックインデックス名を指定して、トピックグラフ用のナレッジソースとナレッジベースを作成します。

   ```powershell
   $v = azd env get-values --output json | ConvertFrom-Json
   $v.PSObject.Properties | ForEach-Object { Set-Item "Env:$($_.Name)" $_.Value }
   uv run scripts/setup_foundry_iq.py --topic-graph --topic-index <topic-index-name>
   ```

   スクリプトが `TOPIC_KNOWLEDGE_SOURCE_NAME` と `TOPIC_KNOWLEDGE_BASE_NAME` を azd 環境に書き込みます。

3. `azd deploy` でエージェントを再デプロイすると、`explore_topics` がトピックグラフ (`topicGraph.kind = diverse` または `balanced`) を使うようになります。

> [!WARNING]
> 自動生成されたインデックスへの手動変更は推奨されていません。トピックグラフ用のプロファイル追加が `content-blob-ks` の更新で上書きされないかを検証してください。問題がある場合は、独自インデックスとインデクサーを作成して `searchIndex` ナレッジソースへ切り替えます。

## 検証事項

* 日次収集で追加された文書がトピック構造に反映されるか (トピックインデックスの再作成頻度)
* リクエスト単位の `topicGraph` 指定 (`diverse` の `diversityFactor`、`balanced`、`none`) による品質・遅延・コストの差
* 特定トピックへの絞り込みはトピックグラフ自体では提供されないため、メタデータ項目と `filterAddOn` で代替できるか
* トピックグラフ検索と通常検索を組み合わせた深掘りフローの総遅延
