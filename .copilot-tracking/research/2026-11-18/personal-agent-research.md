<!-- markdownlint-disable-file -->
# Personal information-gathering agent: research (2026-11-18)

## Scope

Build the agent described in docs/requires.md: MAF hosted agent (East US 2), serverless Foundry IQ (Japan East), Blob pull ingestion, Cosmos DB serverless, Functions at 06:00 JST, web search, HTML reports, source candidate approval. Voice Live deferred (requirement step 9).

## Verified findings

* Serverless Foundry IQ exists: `Microsoft.Search/searchServices` `sku.name: serverless`. ARM in Japan East accepts api-version `2026-09-01-preview` (not `2026-11-01`, NoRegisteredProviderFound).
* `azureBlob` knowledge source auto-creates datasource/skillset/index/indexer (`<ks>-indexer`, `<ks>-index`). No "run KS" API; run the generated indexer (`POST /indexers('<name>')/run`), 202 = started, 409 = running. Default `synchronizationInterval` is `1d`.
* `chatCompletionModel` must not be set when `disableImageVerbalization` is true (400).
* Knowledge base retrieve (`2026-10-01`): `messages` (low/medium effort) or `intents` (minimal), `knowledgeSourceParams[]` with `kind` matching the source. KB models on GA API: gpt-5.x family (gpt-6.x only on `2026-11-01-preview`), so `gpt-5.4-mini` is the KB model.
* Topic graph (preview `2026-11-01-preview`) only works on `searchIndex` knowledge sources and needs a topic index prepared by the `foundry-iq-skills` Copilot CLI plugin. Per-request `topicGraph: {kind: diverse|balanced|none}`. No topic filter; no documented refresh.
* foundry-iq-skills `topic-graph-build` (plugin source: microsoft/GitHub-Copilot-for-Azure plugins/foundry-iq-skills) runs via `bootstrap.py run topicgraph`; flow doctor -> inspect -> plan (approval card) -> apply -> verify -> plan-retrieval (approval card) -> configure-retrieval -> verify-retrieval. Native binary from Azure/foundry-iq-tool-releases (win-x64 OK). The azureBlob-generated index (uid/snippet/blob_url/snippet_vector, cosine HNSW, stored+retrievable) is admitted; the binding adds `vectorSearch.topicGraphProfiles` and the field's `topicGraphProfile` and survives KS re-PUT and indexer runs.
* `infra.provider: microsoft.foundry` compiles any Bicep under `infra/` as its own template; supporting Bicep lives in `support-infra/`, deployed by the postprovision hook.
* ARM camel-cases output names (`SEARCH_ENDPOINT` -> `searcH_ENDPOINT`); azd `${VAR}` substitution is case-sensitive, so outputs are upper-cased before `azd env set`.
* `azd ai agent show --output json` exposes `instance_identity.principal_id` (assignable) and `blueprint.principal_id` (not assignable: PrincipalTypeNotSupported).
* MAF: `FoundryChatClient.get_web_search_tool()` works in the hosted agent; `user_location.timezone` fails on Windows without tzdata (pydantic ZoneInfo). Web search content arrives as `search_tool_call` with `action.query/sources`, URL citations as text annotations. Tools can take `FunctionInvocationContext` (session state); agent middleware can add `stream_result_hooks`.
* `gpt-6.1-sol` at 100K TPM hit 429 during a web-search turn; raised to 250K (quota max).
* Local environment: OpenSSL clients (Python, azd Go client) intermittently get TLS EOF to `*.services.ai.azure.com` / `*.cognitiveservices.azure.com` while Schannel works; retry or test remotely.
