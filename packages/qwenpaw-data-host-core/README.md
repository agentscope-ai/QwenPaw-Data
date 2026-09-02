# qwenpaw-data-host-core

Shared host foundation for QwenPaw Data.

This package owns the non-HTTP QwenPaw Data host runtime:

- `QwenPawDataHost` runtime handle for `plan`, `execute`, and `run`
- AgentScope agent, toolkit, workspace tools, and prompt templates
- DAG orchestration, session trace storage, and artifact path helpers
- MCP Context Manager timeout and metadata injection helpers
- Context Manager HTTP discovery client used by CLI integrations

Concrete host packages such as `qwenpaw-data-cli` should depend on this package instead of duplicating host orchestration logic.

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
| `POST /sessions/{sid}/chats/{cid}/steer` | Inject steering text into the running turn (204; 409 when not active) |
| `POST /sessions/{sid}/chats/{cid}/clarification/answer` | Deliver an `ask_user_question` result to the paused turn |
| `GET/PUT/DELETE /preferences/providers[/{id}]` · `.../models/{id}` · `GET/PUT /preferences/active-models` | Model provider credentials (masked in responses), model overrides, active model selection |
| `GET /datasources` | DataBridge datasource discovery proxied through the service |
| `GET/POST /cron/jobs` · `GET/PUT/DELETE /cron/jobs/{id}` · `POST .../pause|resume|run` | Scheduled agent runs: a cron expression or one-shot time opens a console chat with the configured message/datasource (single-process APScheduler) |

### Storage backends

The service persists sessions/chats/events/preferences through a store
protocol with two interchangeable backends:

- **sqlite (default)**: `<home>/host/host.db` (WAL); point
  `QWENPAW_DATA_DB_URL` at another SQLAlchemy async URL (e.g. postgres).
- **JSON files**: set `QWENPAW_DATA_STORE=json` for the zero-dependency
  file layout (`<home>/host/{chats,sessions,preferences}/`).

A conformance test suite runs every store test against both backends.
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
- `QWENPAW_DATA_STORE` — `json` to use file-backed stores (default: sqlite)
- `QWENPAW_DATA_DB_URL` — SQLAlchemy async URL (default `sqlite+aiosqlite:///<home>/host/host.db`)
- `QWENPAW_DATA_PREFS_MASTER_SECRET` — hex secret (≥32 bytes) to encrypt stored provider API keys at rest
- `QWENPAW_DATA_CORS_ALLOW_ORIGINS` — comma-separated origins (default loopback)
- `QWENPAW_DATA_STREAM_SSE_HEARTBEAT_SECONDS` — keepalive interval (default 15)
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
