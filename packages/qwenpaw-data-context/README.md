# qwenpaw-data-context

The **context management + graph memory foundation** of QwenPaw Data. It organizes
data metadata, topology, and business knowledge into a connected, retrievable,
and evolvable graph memory, exposing only semantic information needs — never
internal graph traversal or vector retrieval topology. Think Mem0 / Zep /
MemOS for LLM applications, with one key difference: this package manages both
fact / episode records and structured nodes such as tables, columns, metrics,
and formulas.

Core architecture: **three graphs** (Metadata Graph / Topology Graph /
Knowledge Graph) + **five stages** (Build → Store → Retrieve → Learn → Govern).

This package was merged from the formerly standalone `context-management`
project and serves as the host-agnostic memory foundation shared by the CLI
host, plugins, and skills.

## Directory layout

```text
packages/qwenpaw-data-context/
├── src/
│   ├── qwenpaw_data/context/          ← QwenPaw Data namespace placeholder (public API reserved)
│   ├── context_manager/          ← CM core: graph build / retrieval / pipelines / FastAPI (api/server.py)
│   └── semantic_config/          ← semantic-config editing layer (SQLite CRUD + Excel import + weave)
├── frontend/                     ← DataBridge frontend (Vite, fixed port 3000)
├── scripts/
│   ├── serve.py                  ← Web / API service entry point
│   └── setup/                    ← graph building, dataset download, vector indexing
├── config/
│   ├── agent_explorer.json       ← Explorer / Agent hyperparameters
│   └── datasources.json          ← datasource registry
├── Makefile                      ← shortcuts (serve / setup)
├── pyproject.toml                ← package definition + dependencies (hatchling)
├── requirements.txt              ← annotated dependency notes
├── requirements.lock.txt         ← verified exact-version snapshot (reproducible installs)
├── semantic_config.db            ← editing-layer SQLite (local, holds connection info, not committed)
└── .venv/                        ← package-scoped uv virtual environment (isolated, not committed)
```

> `frontend/` and the API belong to the same DataBridge local runtime and are
> initialized and started together by the repository scripts.

## Prerequisites

- **Python 3.12** (see `.python-version`)
- **[uv](https://docs.astral.sh/uv/)** (virtual environments and dependency management)
- **Node.js + npm** (DataBridge frontend)
- **Neo4j 5.20+ Community** (graph store; optional, only needed for graph capabilities)

> The service starts even when the graph store / PG is down: `GET /api/health`
> returns a minimal liveness status, and the **SQLite**-based semantic-config
> editing layer (`/api/semantic-config/*`) supports CRUD without the graph
> store.

### Start the database (optional, Docker)

```bash
# Neo4j
export NEO4J_PASSWORD="$(openssl rand -hex 32)"
docker run -d --name neo4j \
  -p 127.0.0.1:7474:7474 -p 127.0.0.1:7687:7687 \
  -e NEO4J_AUTH="neo4j/${NEO4J_PASSWORD}" \
  -e NEO4J_PLUGINS='["apoc"]' \
  -e NEO4J_dbms_security_procedures_unrestricted='apoc.*' \
  neo4j:5.20-community
```

## Installation

The recommended way is to initialize DataBridge Python and frontend
dependencies from the repository root:

```bash
scripts/init_databridge.sh
```

To install the package-scoped uv virtual environment manually, run the
following inside `packages/qwenpaw-data-context/`:

```bash
# 1) Create an isolated virtual environment (Python 3.12)
uv venv --python 3.12 .venv

# 2) Install dependencies
VIRTUAL_ENV="$(pwd)/.venv" uv pip install -r requirements.lock.txt

# 3) Register this package in editable mode (context_manager / semantic_config / qwenpaw_data.context)
VIRTUAL_ENV="$(pwd)/.venv" uv pip install -e . --no-deps
```

> To re-resolve dependencies from declared constraints (instead of the locked
> snapshot), use `uv pip install -e .`, which follows `dependencies` in
> `pyproject.toml`.

### Configure environment variables

```bash
cd ../..
cp .env.example .env
```

Edit `.env` at the repository root and fill in the model configuration:

```bash
OPENAI_API_KEY=sk-xxx
OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_MODEL=qwen-max
EMBED_MODEL=text-embedding-v3
EMBED_DIM=1024
```

Replace `NEO4J_PASSWORD=YOUR_PASSWORD` in `.env` with your local Neo4j
password. The local Neo4j URI and username have working defaults; set
`NEO4J_URI` and `NEO4J_USER` only when connecting to an external instance.

> Datasource connection details (PostgreSQL / MySQL / ODPS / Hologres, etc.)
> are managed in the semantic-config layer (`/api/semantic-config/datasource`)
> and do not need to be set in `.env`.

## Start the service

```bash
# Recommended: start the DataBridge frontend and API together
scripts/start_databridge.sh

# Start only the databases and the API
scripts/start_databridge.sh --skip-frontend
```

On a successful start you will see:

```text
INFO api.server: Neo4j driver opened: bolt://localhost:7687
INFO api.server: semantic-config SQLite initialized
INFO:     Uvicorn running on http://127.0.0.1:8765
```

Default service addresses:

```text
DataBridge UI:      http://localhost:3000
DataBridge API:     http://localhost:8765
OpenAPI:            http://localhost:8765/docs
```

The frontend is served by Vite with hot reload on the fixed port 3000; if the
port is occupied, startup fails explicitly instead of silently switching to
another port.

Common flags: `--reload` (backend source hot reload), `--host`, `--log-level`,
`--skip-frontend`. Frontend source updates are always handled by Vite HMR.

## API overview

The service hosts three route families in a single process on one port
(default 8765).

### CM semantic capabilities (REST prefix `/api/v1/cm`, MCP prefix `/mcp/v1/cm`)

L1 — intent understanding:

| Endpoint | Method | Description |
|------|------|------|
| `/search_context` | POST | Natural language → structured semantic context (SSE streaming) |

L2 — context operations: `/explore_entity`, `/compare_entities`,
`/search_event`, `/execute_sql` (all POST).

L3 — entity queries (GET): `/domains`, `/domain-overview`, `/metrics`,
`/search-metrics`, `/north-star-metrics`, `/dimensions`,
`/dimension-hierarchy`, `/dimension-values`, `/datasets`,
`/dataset-relations`.

### Semantic-config editing layer (prefix `/api/semantic-config`)

Manages datasources, business domains, datasets, dimensions, metrics, and
related entities in local **SQLite** (CRUD + Excel import), then pushes the
configuration into the CM graph through "weave". Runs in the same process and
port as CM.

- Main routes: `/datasource`, `/biz-domain`, `/dataset-meta`,
  `/dataset-column-meta`, `/dataset-dimension`, `/dimension`, `/metric-lib`,
  `/metric-formula-lib`, `/import/excel`, `/weave-task/*`.
- **Weave**: `POST /weave-task/submit` assembles the semantic payload for a
  whole datasource and calls CM's semantic import logic in-process
  (`context_manager.graph.semantic_import_service.run_semantic_import_async`)
  to write into the graph store; the callback address is configured via
  `WEAVE_CALLBACK_URL` in `.env`.
- Storage does not depend on Neo4j/PG: CRUD and Excel import work even without
  the graph store online (only "weave" requires it).
- Error protocol: `/api/semantic-config/*` returns
  `{timestamp,status,error,message}`; CM `/api/v1/*` keeps its own protocol.
- Positioning: designed for **local-first, single-user deployments**. All API
  routes require Bearer token authentication (`QWENPAW_DATA_API_TOKEN` or scoped
  `QWENPAW_DATA_API_KEYS`); only liveness endpoints are exempt. Review the security
  model in the repository root README before exposing anything beyond
  `127.0.0.1`.

### Graph browsing / operations / exploration (prefix `/api`)

`/api/health`, `/api/agent_query`, `/api/chat_stream`, `/api/execute_sql`,
`/api/global_graph`, `/api/domain_graph`, `/api/search_nodes`, and more (used
by the frontend pages and scripts). See `/docs` for the full list.

## Paths and configuration notes (post-merge)

- Importable backend packages live in `src/` (`context_manager`,
  `semantic_config`); operational directories and assets (`scripts/`,
  `config/`, `semantic_config.db`) live at the package root; environment
  variables are read from the repository root `.env`.
- Path derivation in `context_manager` and `semantic_config` is anchored to
  the **package root** (absolute paths based on `__file__`), independent of
  the startup directory (CWD), so the service can be started from anywhere.
- `.venv/`, the repository root `.env`, and `*.db` files are ignored via
  `.gitignore` (`semantic_config.db` holds datasource connection info — do
  not commit it).

## Bundled demo assets

`qwenpaw-data-context` ships a self-contained GAAP demo for the docker-compose
one-shot setup in the main QwenPaw repository:

- `context_manager/demo/assets/demo_semantic_config.xlsx` — semantic-layer workbook
- `context_manager/demo/assets/seed-postgres.sql` — idempotent PostgreSQL seed script

These assets are accessible from a PyPI install via `importlib.resources`:

```python
from context_manager.demo.loader import seed_postgres_sql, semantic_workbook_bytes

sql = seed_postgres_sql()
workbook_bytes = semantic_workbook_bytes()
```

See [docs/demo.md](docs/demo.md) for the asset lifecycle and regeneration
instructions.
