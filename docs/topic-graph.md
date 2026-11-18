---
title: トピックグラフの構成と再生成
description: Foundry IQ のトピックグラフ (プレビュー) を通常検索とは独立した知識ベースとして構成・再生成する手順と検証結果
ms.date: 2026-11-18
ms.topic: how-to
---

## 構成

トピックグラフは `searchIndex` 種別のナレッジソースでのみ利用できるため、通常検索とは別の知識ベースとして構成しています。

| 用途         | ナレッジソース                   | ナレッジベース       | 対象インデックス                                       |
|--------------|----------------------------------|----------------------|--------------------------------------------------------|
| 通常検索     | `content-blob-ks` (`azureBlob`)  | `personal-kb`        | `content-blob-ks-index` (Blob 取り込みで自動生成)      |
| トピック探索 | `content-topic-ks` (`searchIndex`) | `personal-topic-kb`  | 同じ `content-blob-ks-index` を参照                    |

トピックインデックス `topicgraph-personal-agent-<generation>` は `foundry-iq-skills` プラグインの `topic-graph-build` CLI で作成します。CLI の `configure-retrieval` がコンテンツインデックスのベクトル項目 `snippet_vector` にトピックプロファイルを結び付けます。保存済みの既定値は `none` で、エージェントの `explore_topics` がリクエストごとに `diverse` または `balanced` を指定します。

`TOPIC_KNOWLEDGE_BASE_NAME` が未設定の環境では、`explore_topics` は通常検索 (`effort=medium`、最大 20 件) で代替し、結果に `mode: explore-fallback` を付けます。

## 初回構成

1. プラグインのランタイムを準備します。GitHub Copilot CLI でプラグインを導入するか、[公開ソース](https://github.com/microsoft/GitHub-Copilot-for-Azure/tree/main/plugins/foundry-iq-skills)を取得して、Python 3.12 から 3.14 で `bootstrap.py setup topicgraph` を実行します。

   ```powershell
   $env:FOUNDRY_IQ_PYTHON = (uv python find 3.13)
   & $env:FOUNDRY_IQ_PYTHON -I "<plugin-root>\graph\runtime\bootstrap.py" setup topicgraph
   ```

2. `searchIndex` 種別のナレッジソースとナレッジベースを作成します。`content-topic-ks` が既に存在する場合は変更しません (`configure-retrieval` の設定を上書きしないため)。

   ```powershell
   $v = azd env get-values --output json | ConvertFrom-Json
   $v.PSObject.Properties | ForEach-Object { Set-Item "Env:$($_.Name)" $_.Value }
   uv run scripts/setup_foundry_iq.py --topic-graph
   ```

3. 下記「トピックの生成と有効化」を実行し、`azd deploy` でエージェントを再デプロイします。

## トピックの生成と有効化

以下では `tgb` を `topic-graph-build` の起動コマンドとして表記します。

```text
tgb = <python> -I <plugin-root>\graph\runtime\bootstrap.py run topicgraph topic-graph-build
```

状態ディレクトリは `.foundry-iq/topicgraph-state` (git 管理外) を毎回使います。書き込みを伴う手順はすべて承認カードの確認が必要です。

1. `doctor`、`inspect`、`plan` で読み取り専用の確認と計画を行います。フィールドは `--key-field uid --text-field snippet --source-field blob_url --vector-field snippet_vector --embedding-model-id text-embedding-3-large` を指定し、トピック名付けのモデルは `plan` に `--model-endpoint https://<account>.openai.azure.com/ --model-deployment gpt-5.4-mini --model-name gpt-5.4-mini` で渡します。
2. 承認後に `plan` が出力した `apply_command` をそのまま実行し、`verify --generation-id <id>` で検証します。
3. `plan-retrieval --generation-id <id> --topic-diversity off --knowledge-source content-topic-ks --knowledge-base personal-topic-kb` で有効化を計画し、承認後に `configure-retrieval --plan-id <id> --confirm-writes` と `verify-retrieval --plan-id <id>` を実行します。`state: configured` になれば完了です。

`publish` はこの PC でトピックを閲覧するためだけの操作で、エージェントの検索には不要です。

## 再生成

日次収集で追加された文書は、トピックインデックスを作り直すまでトピックに属しません。チャンク数が大きく増えたとき (目安として 2 倍、または推奨値の 2,000 件に達したとき) に、上記「トピックの生成と有効化」を新しい世代で再実行してください。旧世代のインデックスは残るため、不要になったら `generations` と `plan-retire` で確認してから削除します。

> [!NOTE]
> 再生成は承認カードによる確認が前提のため、Functions による自動化は行っていません。

## 検証結果 (2026-11-18)

* 149 チャンクから 7 トピックを生成しました (カバレッジ 1.0、名付けは gpt-5.4-mini で 7 回呼び出し、ビルド約 4 分)
* 推奨の 2,000 チャンクを下回るため、トピックはまだ粗い分類です
* 同じ質問で `topicGraph` を `none`、`balanced`、`diverse` に切り替えると返る根拠の構成が変わり、`diverse` では異なる文書の組み合わせになりました。データ量が少ないため、品質向上の有無はまだ判断できません
* 広い質問「収集した情報全体のテーマや傾向」に対し、通常検索 (`personal-kb`) は根拠 0 件、トピック探索 (`personal-topic-kb`) は 20 件を返しました。応答時間はトピック探索が約 6 から 7 秒、通常検索が約 4 秒です
* `setup_foundry_iq.py` の再実行 (Blob ナレッジソースの更新) とインデクサー実行の後も、コンテンツインデックスのトピック結び付けは維持されました

## 残る検証事項

* データ量を増やした後の品質・遅延・コストの比較 (評価スイートで `none` と `diverse` を比較)
* 特定トピックへの絞り込みはトピックグラフでは提供されないため、メタデータ項目と `filterAddOn` で代替できるか
* Blob ナレッジソースの定義変更 (埋め込みモデル変更など) でインデックスが再作成された場合の再構成手順
