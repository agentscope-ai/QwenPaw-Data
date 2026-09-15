# QwenPaw Data App 0.3.0 — Release Preview

[中文预告](QWENPAW_APP_ZH.md) · [Repository overview](../README.md) ·
[Integration PR #7637](https://github.com/agentscope-ai/QwenPaw/pull/7637)

> **Planned App release · Release date: to be announced.**
> As of 2026-09-15, the integration is under review in #7637 and has not merged.

The upcoming QwenPaw Data App brings a complete data-analysis workflow into
QwenPaw: select a datasource, ask a business question, clarify the request,
follow execution, read the report, and continue with follow-up questions.

QwenPaw users gain a specialized analysis workspace and data-analysis entry
points in their channel conversations. QwenPaw supplies app lifecycle,
host authentication, channels, and notifications; QwenPaw-Data supplies
semantic grounding, analytical skills, execution, and the Data Console.
The engine also runs independently, with QwenPaw as an optional host.

## Highlights

- **A complete analysis workspace.** The embedded Data Console connects
  datasource selection, analysis sessions, execution traces, and outputs.
- **An interactive analysis process.** Follow streamed progress, answer
  clarification questions, cancel a run, and continue the analysis with
  follow-up questions.
- **Reports in context.** Generated HTML reports can be previewed alongside the
  analysis conclusion, with artifact access and download options retained.
- **QwenPaw channel entry points.** Use `/data` to enter data-analysis mode and
  `/datasource` to select a datasource. The bridge maintains session mappings
  and delivers clarification, results, and artifact notifications.
- **Managed or external services.** Let the App manage Engine and Context
  services, or connect services operated separately.

These highlights describe the incoming App update in #7637. Availability in a
QwenPaw installation depends on the App build it contains.

## From a business question to a report

For a request such as **“Analyze product X's March performance and explain the
changes,”** the workflow is:

1. Select a registered datasource in the Data workspace, or enter `/data` and
   use `/datasource` from a supported QwenPaw channel conversation.
2. Ask the question and answer clarification requests about metrics or scope.
3. Follow the analysis as DataBridge resolves business concepts and the engine
   executes the analytical work; cancel the run when needed.
4. Read the conclusion and generated report in the workspace, inspect the
   output files, and continue with a follow-up question. Channel users receive
   the result and artifact notifications through the bridge.

## Configuration and setup

The preview uses the Data Console's existing configuration pages:

| Setting | Entry point |
| --- | --- |
| Analysis model providers, credentials, active models, and runtime settings | Data Console → Settings → Agent Configuration |
| DataBridge semantic-service model, embedding model, and Neo4j store | Data Console → Settings → DataBridge Configuration |
| SQL datasource connections | Data Bridge → Data Sources in the linked Context console |

SQL connections live in DataBridge's datasource registry. Saving Neo4j or model
settings does not register a SQL datasource or write SQL credentials into `.env`.
The semantic-service model and the analysis-agent model serve different roles.

The App source and build instructions live in the **QwenPaw** repository.
The links below pin the PR's source at `0b64503d` so the preview instructions
remain distinct from released QwenPaw packages:

- [App README and setup instructions](https://github.com/cyruszhang/QwenPaw/blob/0b64503d2ada70f1b3842ebb7f3f4e13fff46997/plugins/apps/qwenpaw-data/README.md)
- [App 中文说明与配置指南](https://github.com/cyruszhang/QwenPaw/blob/0b64503d2ada70f1b3842ebb7f3f4e13fff46997/plugins/apps/qwenpaw-data/README_ZH.md)
- [App source snapshot](https://github.com/cyruszhang/QwenPaw/tree/0b64503d2ada70f1b3842ebb7f3f4e13fff46997/plugins/apps/qwenpaw-data)
- [QwenPaw project](https://github.com/agentscope-ai/QwenPaw)
- [Standalone engine HTTP/SSE setup](../packages/qwenpaw-data-host-core/README.md#headless-service-optional-service-extra)

For the preview, use a QwenPaw build containing #7637 and follow its App setup
instructions. Installing the Python runtime packages supplies service
dependencies; it does not replace the QwenPaw App source or UI bundle.

## Versions and availability

This announcement concerns the incoming **App** release. The engine's `v0.3.0`
and `v0.3.1` releases are already published.

| Component | Status | Details |
| --- | --- | --- |
| QwenPaw Data App 0.3.0 | Planned; integration under review, release date to be announced | [QwenPaw #7637](https://github.com/agentscope-ai/QwenPaw/pull/7637) |
| QwenPaw-Data Engine / Context / CLI / Skills 0.3.0 | Published; introduces the coordinated 0.3 runtime line | [Engine 0.3.0 release](https://github.com/agentscope-ai/QwenPaw-Data/releases/tag/v0.3.0) |
| QwenPaw-Data packages 0.3.1 | Published patch with clarification/cron agent tools and execution fixes | [Engine 0.3.1 release](https://github.com/agentscope-ai/QwenPaw-Data/releases/tag/v0.3.1) |

**0.3.0 is the planned App release label used by #7637.** The App manifest at
the linked source snapshot still declares `0.1.2`; the bundle version needs
alignment before publishing. The final announcement will identify the App
bundle, supported QwenPaw version, tested engine baseline, and installation
entry point.

Silent-command observations and persistence before terminal events are engine
**0.3.1** fixes. The App's `>=0.3,<0.4` dependency range also permits `0.3.0`;
check the installed package versions when verifying these fixes.

## Scope and known limitations

- **Semantic-knowledge settlement is not a complete supported App workflow.**
  The engine and console contain settlement components, but candidate validation
  and confirmed-card writeback depend on a Context `feedback_card` contract that
  the public Context service does not expose. Do not rely on the preview for
  automatic knowledge-card generation and writeback. This also qualifies the
  settlement highlight in the engine 0.3.0 release notes.
- **PawApp vNext is separate future work.** Shared Host Skill/Tool access,
  App-scoped capability contracts, and a unified Host configuration protocol are
  not delivered by this App integration. The configuration pages above remain
  the applicable user instructions.

Follow #7637 for integration progress; use the
[engine changelog](../CHANGELOG.md) for Python package changes.
