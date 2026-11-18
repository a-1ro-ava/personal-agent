<!-- markdownlint-disable-file -->
# Changes: personal information-gathering agent (2026-11-18)

Related research: ../../research/2026-11-18/personal-agent-research.md

## Summary

Scaffolded the hosted agent with `azd ai agent init` (MAF Responses local-tools sample), implemented the agent, collector, supporting infrastructure and hooks, provisioned everything in `rg-personal-agent`, deployed agent version 2 and the Function App, and verified end to end.

## Added

* azure.yaml (models gpt-6.1-sol 250K / gpt-5.4-mini / text-embedding-3-large; hooks predeploy/postprovision/postdeploy)
* support-infra/main.bicep
* scripts/postprovision.ps1, scripts/postdeploy.ps1, scripts/AzdEnv.psm1, scripts/setup_foundry_iq.py, scripts/seed_sources.py, scripts/sync_shared.py
* config/sources.seed.json
* src/shared/pa_shared/ (vendored into src/agent and src/functions)
* src/agent/main.py, src/agent/agent_core/{config,knowledge,store,reports,tools,activity_log,instructions}.py, src/agent/tests/
* src/functions/{function_app.py,host.json,requirements.txt,.funcignore}, src/functions/collector/, src/functions/tests/
* .vscode/{tasks,launch}.json, docs/topic-graph.md, README.md, AGENTS.md

## Deviations

* GitHub seed `microsoft/agent-framework` returned 404 from the GitHub API; replaced by `Azure/azure-dev` and the original source disabled with `disabledReason`.
* Voice Live not implemented (requirement step 9, conversation-engine route availability in East US 2 unverified).
* Topic graph not enabled yet (needs topic index via foundry-iq-skills plugin); agent falls back to broad search.

## Validation

* pytest: src/agent 19 passed, src/functions 8 passed
* Local collector run: 41 new docs, indexer success (41 processed, 0 failed)
* Remote agent: knowledge search with citations + HTML report (SAS link served text/html with CSP); web search + candidate proposal; approval registered source with decisionNote; logs in conversations/searchlogs
* Deployed Function `collect_now`: 6 sources, 11 new, 41 unchanged, indexer started

## Follow-up: topic graph enabled (2026-11-18, agent version 3)

* Built topic index `topicgraph-personal-agent-8c3348634c00` (7 topics, 149 chunks, coverage 1.0, labels by gpt-5.4-mini) with foundry-iq-skills `topic-graph-build` after explicit approval of its build card
* Created `content-topic-ks` (searchIndex over `content-blob-ks-index`, returns snippet/blob_url/snippet_parent_id) and `personal-topic-kb`; bound topics with `configure-retrieval` (stored default `none`, separate approval); `verify-retrieval` state configured
* scripts/setup_foundry_iq.py: `--topic-graph` no longer needs `--topic-index`; `content-topic-ks` is create-only so the CLI-owned `topicGraph` setting is preserved
* Binding persisted across a `setup_foundry_iq.py` re-run and an indexer run
* Live retrieval: broad question returned 20 refs via topic KB vs 0 via normal KB; none/balanced/diverse change evidence mix; quality gain not measurable at 149 chunks
* Added test for the per-request topic graph request; src/agent 21 passed; deployed agent version 3 and verified remote `explore-diverse` answer with citations
* docs/topic-graph.md rewritten (setup, regeneration, results); README and AGENTS.md updated
