# Coding Agent Instructions

This project is a personal information-gathering agent built with Microsoft Agent Framework and hosted as a **Microsoft Foundry hosted agent**. Requirements are in `docs/requires.md`; the architecture and operations are in `README.md`.

This project was built with the microsoft-foundry skill. Before working on or answering questions about foundry agents, read the microsoft-foundry skill first. If you are in VS Code, read the vscode-microsoft-foundry skill first.

## Layout

* `azure.yaml`: azd project: Foundry project, model deployments, hosted agent `personal-agent`, hooks
* `src/agent/`: hosted agent (`main.py`, `agent_core/`), tests in `src/agent/tests`
* `src/functions/`: daily collector Function App (timer 21:00 UTC = 06:00 JST), tests in `src/functions/tests`
* `src/shared/pa_shared/`: source of truth for shared code; copies in `src/agent/pa_shared` and `src/functions/pa_shared` are refreshed by `uv run scripts/sync_shared.py` (never edit the copies)
* `support-infra/main.bicep`: supporting resources deployed by `scripts/postprovision.ps1` (kept out of `infra/` because the `microsoft.foundry` provider compiles any Bicep under `infra/`)
* `scripts/`: azd hooks, Foundry IQ setup, source seeding

## Development workflow

Set `AZURE_DEV_USER_AGENT=microsoft_foundry_skill` inline when running azd commands from an agent session.

```bash
azd ai agent run --no-client               # Run locally on http://localhost:8088 (from src/agent with its .venv active)
azd ai agent invoke --local "your message" # Test the local agent
azd deploy                                 # Deploy the agent and (postdeploy) the Function App
azd ai agent invoke "your message"         # Invoke the deployed agent
```

Tests:

```bash
cd src/agent && .venv/Scripts/python -m pytest -q tests
cd src/functions && .venv/Scripts/python -m pytest -q tests
```

## Conventions

* New agent settings are read in `src/agent/agent_core/config.py` and must be added to the agent `env` block in `azure.yaml`.
* Bicep output names are upper-cased by `postprovision.ps1` before `azd env set`; azd `${VAR}` substitution is case-sensitive.
* Collection targets are only registered through `approve_source_candidate`, which requires the user's approval to be quoted from their latest message.
* Topic graph retrieval uses `content-topic-ks`/`personal-topic-kb` over the generated content index. Topic indexes are built and bound only with the foundry-iq-skills `topic-graph-build` CLI and its approval cards (state in the git-ignored `.foundry-iq/topicgraph-state`); never hand-edit the topic binding or `content-topic-ks.topicGraph`. See `docs/topic-graph.md`.

## References

* [Hosted agents overview](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents)
* [Microsoft Foundry Skill](https://learn.microsoft.com/en-us/azure/foundry/how-to/develop/use-microsoft-foundry-skill)
