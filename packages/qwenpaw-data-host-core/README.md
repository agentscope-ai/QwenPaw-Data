# qwenpaw-data-host-core

Shared host foundation for QwenPaw Data.

This package owns the non-HTTP QwenPaw Data host runtime:

- `QwenPawDataHost` runtime handle for `plan`, `execute`, and `run`
- AgentScope agent, toolkit, workspace tools, and prompt templates
- DAG orchestration, session trace storage, and artifact path helpers
- MCP Context Manager timeout and metadata injection helpers
- Context Manager HTTP discovery client used by CLI integrations

Concrete host packages such as `qwenpaw-data-cli` should depend on this package instead of duplicating host orchestration logic.

This first migration stage intentionally leaves HTTP server and router code out.

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
