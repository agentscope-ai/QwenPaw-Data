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
| `POST /sessions/{session_id}/chats` | Start a turn: `{"text": ..., "datasource_id": ...}` → `{"chat": {...}}`; 409 while a turn is active |
| `POST /sessions/{session_id}/chats/{chat_id}/stop` | Cancel a running turn |
| `GET /sessions/{session_id}/chats/{chat_id}/events` | SSE stream: replays persisted history, then live events; supports `Last-Event-ID` (or `after_sequence_number`) resume |

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
- `QWENPAW_DATA_CORS_ALLOW_ORIGINS` — comma-separated origins (default loopback)
- `QWENPAW_DATA_STREAM_SSE_HEARTBEAT_SECONDS` — keepalive interval (default 15)
- `QWENPAW_DATA_MODEL_PROVIDER` / `_NAME` / `_API_KEY` / `_BASE_URL` — model config

Chat and event history persist under
`${QWENPAW_DATA_HOME:-~/.qwenpaw-data}/host/chats/` as JSON/JSONL, so SSE
resume works across service restarts; orphaned running chats are cancelled
on startup.

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
