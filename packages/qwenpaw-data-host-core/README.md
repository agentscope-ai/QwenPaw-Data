# qwenpaw-data-host-core

Shared host foundation for QwenPaw Data.

This package owns the QwenPaw Data host service and its Python client:

- `HostClient` for remote `plan`, dependency-ordered `execute`, and `run`
- AgentScope agent, toolkit, workspace tools, and prompt templates
- SQL-backed sessions, event streams, and artifact path helpers
- MCP Context Manager timeout and metadata injection helpers
- Context Manager HTTP discovery client used by CLI integrations

Concrete host packages such as `qwenpaw-data-cli` should depend on this package instead of duplicating host orchestration logic.

When a WeChat conversation produces an image or file, the bot sends its name
as text. Open that conversation in QwenPaw Data Console and select **Session
artifacts** (会话产物) to view it. The bot does not upload a copy to WeChat or
create a public download link.

## Headless service (optional `[service]` extra)

The engine can run as a headless service that any frontend (PawApp, CLI,
third parties) consumes over HTTP + SSE:

```bash
pip install "qwenpaw-data-host-core[service]" uvicorn
QWENPAW_DATA_API_TOKEN=<token> \
uvicorn --factory 'qwenpaw_data.host.core.api.app:create_app' --workers 1
```

The service is single-process (in-process event fan-out); always run one
worker. Without `QWENPAW_DATA_API_TOKEN`, only loopback clients are accepted.

### API surface (`/api/v1`)

| Route | Purpose |
|---|---|
| `POST /sessions` · `GET /sessions` · `GET/PATCH/DELETE /sessions/{id}` | Session lifecycle: create, list (search/filter/sort/pagination), rename, soft-delete |
| `POST /sessions/{sid}/chats` | Start a turn in an existing session: `{"text": ..., "datasource_id": ...}` → `{"chat": {...}}`; 404 without a session, 409 while a turn is active |
| `GET /sessions/{sid}/chats` | List a session's chats |
| `POST /sessions/{sid}/chats/{cid}/stop` | Cancel a running turn |
| `GET /sessions/{sid}/chats/{cid}/events` | SSE stream: replays persisted history, then live events; supports `Last-Event-ID` (or `after_sequence_number`) resume |
| `POST /sessions/{sid}/chats/{cid}/steer` | Commit steering text/comments to the SQL command queue (200; 409 when not active) |
| `POST /sessions/{sid}/chats/{cid}/clarification/answer` | Commit an `ask_user_question` result for runtime consumption (200; 404 when the chat is not active) |
| `GET/PUT/DELETE /preferences/providers[/{id}]` · `.../models/{id}` · `GET/PUT /preferences/active-models` | Model provider credentials (masked in responses), model overrides, active model selection |
| `GET /datasources` | DataBridge datasource discovery proxied through the service |
| `GET/POST /cron/jobs` · `GET/PUT/DELETE /cron/jobs/{id}` · `POST .../pause|resume|run` | Scheduled agent runs: a cron expression or one-shot time opens a console chat with the configured message/datasource (single-process APScheduler) |

### Python service client

`qwenpaw_data.host.core.client.HostClient` connects to an existing Host through
the same authenticated API as Console. It does not start a service, change model
preferences, or replace the old in-process `QwenPawDataHost` embedding contract.

```python
from contextlib import aclosing
from qwenpaw_data.host.core.client import HostClient

async with HostClient("http://127.0.0.1:8000", token=host_token) as host:
    session_id = await host.create_session()
    async with aclosing(host.run(session_id, "Summarize sales")) as events:
        async for event in events:
            print(event)
```

Tokens are explicit, not loaded from environment by the library. Use HTTPS for
remote deployments. `events(session_id, chat_id, after_sequence_number=N)` can
replay persisted events without starting or stopping a turn. Consume `run` to
its terminal event: failed/cancelled or truncated streams raise `HostClientError`.
Closing a partial `run` stream requests cancellation; the server owns actual
execution cleanup. Session history is retained. Interactive integrations must
handle tool-confirmation events via `answer_tool_confirmation`; new sessions
disable clarification by default. Admission POSTs are never retried automatically.

`await host.plan(session_id, prompt)` returns a validated
`qwenpaw_data.host.core.domain.sop.SOP` through
`POST /api/sessions/{session_id}/sop/plan`. This is prompt-only planning: it uses
the session's authorized model preferences but does not start a Chat, allocate
an execution workspace, inspect datasource schemas or call agent tools. The
request is bounded to 32,768 characters and the model call to 60 seconds.
Duplicate node IDs, unknown/cyclic dependencies and runtime state fields are
rejected. The SOP can be serialized as YAML.

`host.execute(session_id, sop)` accepts a SOP, dictionary or YAML string and
returns an async event stream. It validates the entire graph before admission,
then executes nodes sequentially in topological order, with one Host turn per
node. Each dependency must finish with a successful terminal response before
its successor starts. Failed/cancelled/truncated streams stop the whole run;
closing the generator stops the active turn. Events include `sop_node_id`, and
completed predecessor text is provided to downstream nodes. Tools, permissions,
credentials and workspace isolation remain owned by the existing Host runtime.

This is a client-side scheduling guarantee, not a model-enforced DAG: the model
cannot advance the scheduler, but its actions within a node remain agentic.
The server's advisory task plan is not used to decide readiness. Completion
does not independently validate business outcomes. There is no parallel node
execution, durable DAG checkpoint/resume, automatic retry or rollback of tool
side effects. Do not rerun an interrupted SOP expecting it to skip completed
nodes. Use a dedicated session to avoid interleaving unrelated conversation
turns; normal server session-conflict checks still apply. The old in-process
`QwenPawDataHost` interface and legacy tool-assisted planner remain unsupported.

The internal service architecture is the migration baseline. The shared
`QwenPawDataAgent` uses `TaskContext` and the service's command-driven executor;
it no longer accepts standalone DAG options (`runtime_state`, `mode`, or
embedding callbacks). This intentionally removes the compatibility extension
introduced in `9b154b7`, rather than maintaining a second execution path.
Use `HostClient` for Python integrations and CLI SOP execution. The service
owns identity, approvals, workspace lifecycle, SQL persistence and trace events.
The SOP document model remains a client-side contract, not a second server
runtime. The retired `host.core.core`, `host.core.registry`, `host.core.model`
modules and CLI `create_qwenpaw_data` factory have been removed; configure
models through the service's provider/preferences APIs. The retired grouped
`agent.toolkit` is also removed; `DataAgentBuilder` assembles the native toolkit.
Remaining legacy modules do not constitute a supported embedding API.

Migration work may break old OSS interfaces. Conflicting OSS architectural
choices require review before replacement; security hardening, safe handling
of existing data, optional infrastructure and OSS packaging remain requirements.

### Clarification and file sharing

Clarification question/option counts are advisory schema descriptions, not hard
array limits. Semantic fields still reject blank values and unknown fields.
Invalid clarification settings fail validation. The native toolkit always
registers `ask_user_question`; when clarification is disabled for a chat, the
executor supplies an explicit user-absent result instead of waiting for a UI.
Steering and clarification APIs commit SQL commands before acknowledging them;
they do not require a runtime in the receiving process. Repeated clarification
answers are queued, not synchronously rejected as already answered.

File sharing (`POST /sessions/{sid}/files/share`) requires a configured Alibaba
OSS bucket and returns an expiring object-store download URL. The historical
local signed-link resolver and `QWENPAW_DATA_SHARE_SECRET` fallback are not
supported. Without object storage, authenticated local preview and download
(`GET /sessions/{sid}/files/access`) remain available. Issuing a share URL
requires write access to the session; possessing that external signed URL
grants download access until it expires, independently of Host authentication.

Compare-picker uploads never overwrite an existing destination, including a
dangling symbolic link. Without directory-descriptor support (notably Windows),
publication requires a filesystem supporting hard links, such as NTFS; unsupported
filesystems fail instead of falling back to an unsafe exclusive-open operation.
This fallback does not provide POSIX directory pinning against concurrent directory
replacement by another process.

### Delegating to a subagent

`spawn_subagent(type, description, prompt)` supports `general` (analysis) and
`report` (report generation from existing evidence; no MCP). Existing OSS
`spawn_subagent(task=..., role="data_fetcher")` calls still return a text summary.
The same Host feature switch, `QWENPAW_DATA_SPAWN_SUBAGENT_ENABLED`, controls both.

Add a Markdown file under the current agent's host-side
`workspace/agents/<agent_id>/` to register a role on the next toolkit build:

```markdown
---
name: evidence-reader
description: Summarize existing session evidence
tools: [Read]
skills: []
mcp: []
---
Read the evidence supplied by the parent. Return the conclusion and its source.
```

Omitting a selector inherits that category from the parent's workspace;
an empty list selects none. Explicit names must already be available (including
enabled skills and session-scoped MCP clients). Duplicate roles and malformed
specifications are rejected. Profiles cannot add a nested delegation tool,
grant permissions, or approve a tool on the user's behalf.

In a native Windows workspace that provides PowerShell instead of Bash, the
shipped `report` and `data_fetcher` presets keep their file tools but get
neither shell. A workspace role that explicitly requests Bash still requires it.

New-style children finish with `submit_result(summary, artifacts)`. The parent
receives one JSON result with `status`, `summary`, and session-relative artifact
references. A file reference does not itself prove the file exists or grant
access. A text-only reply is not a successful handoff. Closing or cancelling
delegation drains the child before releasing its concurrency slot.

### Storage backends

The current service uses SQL repositories backed by the shared control-plane
database. SQLite is the default; `QWENPAW_DATA_DB_URL` can select PostgreSQL.

**Open compatibility gap:** the legacy JSON stores remain in the source tree,
but are not connected to the current HTTP runtime. `QWENPAW_DATA_STORE=json`
now stops startup before database initialization instead of silently using SQL.
Existing JSON data is neither imported nor deleted. Keep the previous compatible
release for that data until JSON support or an explicit migration is available;
do not unset the variable expecting old sessions to appear. This is a protective
check, not retirement of the JSON compatibility requirement.

When no explicit model is passed, the service resolves the local user's
configured active model from preferences and falls back to env vars.

### SSE stream objects

Each SSE frame carries `id` (dense per-chat sequence number), `event`
(object type), and a JSON body discriminated on `object`:

`response` (created/in_progress/completed/failed/cancelled, terminal frames
close the stream) · `message` / `content` (assistant text, reasoning, tool
calls and outputs, media) · `task_status` (DAG plan snapshots) ·
`biz_event` / `segment` / `artifact.registered` / `followup.generated`
(reserved; producers arrive in later waves).

### Environment variables

- `QWENPAW_DATA_API_TOKEN` — bearer token; unset = loopback-only
- `QWENPAW_DATA_STORE` — legacy selector; `json` is rejected until compatibility is restored
- `QWENPAW_DATA_DB_URL` — SQLAlchemy async URL (default: shared local control-plane database)
- `QWENPAW_DATA_WORKSPACE_BACKEND` — `docker` by default; `local` is explicit, unsandboxed opt-in
- `QWENPAW_DATA_PREFS_MASTER_SECRET` — hex secret (≥32 bytes) to encrypt stored provider API keys at rest
- `QWENPAW_DATA_CORS_ALLOW_ORIGINS` — comma-separated origins (default loopback)
- `QWENPAW_DATA_STREAM_SSE_HEARTBEAT_SECONDS` — keepalive interval (default 15)
- `QWENPAW_DATA_FOLLOWUP_ENABLED` — `0` disables follow-up question recommendation (default on)
- `QWENPAW_DATA_MODEL_PROVIDER` / `_NAME` / `_API_KEY` / `_BASE_URL` — model config fallback when no active model is set via preferences

Chat and event history persist in the selected backend, so SSE resume
works across service restarts; orphaned running chats are cancelled on
startup.

## Default local state

`qwenpaw-data-host-core` resolves its own local state through
`qwenpaw_data.host.core.paths`; it does not use DataBridge path helpers.

```text
${QWENPAW_DATA_HOME:-~/.qwenpaw-data}/host/
├── .secrets/                              # shared durable Host secrets
└── workspace/
    ├── .mcp                               # shared durable MCP config
    ├── skills/                            # shared durable skills
    ├── sessions/
    │   ├── console/                       # session traces
    │   ├── dag/                           # session DAG snapshots
    │   └── {session_id}/                  # AgentScope context/tool offload
    ├── data/                              # shared durable AgentScope blobs
    └── artifacts/
        └── {session_id}/                  # direct-chat artifacts
            └── {graph_id}/{node_id}/      # optional TaskGraph node scope
```

Directories are created lazily by their writers. The Host does not read or
migrate the historical `${QWENPAW_DATA_HOME}/agents/default` layout.
