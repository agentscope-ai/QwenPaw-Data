# QwenPaw-Data: Bridging Facts, Methodology, and Execution for Autonomous Enterprise Data Analytics

[中文 README](./README_ZH.md)

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](./LICENSE)
[![PyPI](https://img.shields.io/pypi/v/qwenpaw-data-cli?label=PyPI)](https://pypi.org/project/qwenpaw-data-cli/)
[![CI](https://github.com/agentscope-ai/QwenPaw-Data/actions/workflows/ci.yml/badge.svg)](https://github.com/agentscope-ai/QwenPaw-Data/actions/workflows/ci.yml)

<p align="center">
  <img src="assets/brand.png" alt="QwenPaw-Data" width="480" />
</p>

**QwenPaw-Data is an agentic data system built for enterprise data analysis.**

Enterprise data analysis is becoming a distinct frontier for autonomous agents. It operates in an open, ambiguous, and continuously evolving environment: business concepts must be grounded to the right entities, analytical procedures must be reproducible despite fuzzy feedback, and long-horizon workflows must execute over real enterprise data while preserving artifacts, provenance, and opportunities for human intervention.

QwenPaw-Data organizes its system design around three core dimensions: **facts**, **methodology**, and **execution**. It consolidates heterogeneous enterprise assets from warehouses, dashboards, business documents, interaction logs, and historical tasks into governed and evolvable analytical context, then turns natural-language requests into end-to-end workflows spanning data understanding, retrieval, analysis, report generation, and decision support.

For a complete system-level overview, see the [Technical Report](https://arxiv.org/pdf/2607.11019).

## Core Idea

The design principle of QwenPaw-Data is to decompose enterprise data analysis according to the core questions an AI-native data agent must answer:

- **What facts to use**: the agent needs governed evidence for business concepts, metrics, dimensions, tables, lineage, and historical context.
- **How to analyze**: the agent needs reusable analytical methodology instead of ad-hoc reasoning for every request.
- **How to run**: the agent needs a controllable runtime for long-horizon, artifact-centric workflows.

QwenPaw-Data implements this decomposition through three collaborative subsystems:

| Subsystem | Role | What it manages |
| --- | --- | --- |
| **DataBridge** | Evidence grounding | Metadata, business knowledge, trace history, metric definitions, data lineage, and task-specific evidence. |
| **Skill-Hub** | Method orchestration | Routing, planning, workflow, and atomic analytical skills, together with references, scripts, and quality expectations. |
| **Host** | Execution control | DAG planning, subagent dispatch, tool invocation, container workspace execution by default, artifact registry, reflection, and recovery. |

Together, the three subsystems provide four data-centric capabilities: trustworthy fact grounding, codified analytical methodology, controllable long-horizon execution, and self-evolving data assets.

## Architecture Overview

<p align="center">
  <img src="assets/architecture.png" alt="QwenPaw-Data architecture overview" width="900" />
</p>

DataBridge turns scattered enterprise sources into a governed semantic substrate. Its metadata graph describes databases, tables, columns, metrics, dimensions, and lineage; its knowledge graph captures business entities, definitions, rules, and organizational context; and its trace graph records task traces, tool usage, intermediate artifacts, user feedback, and reusable experience.

Skill-Hub sits above this substrate as the method subsystem. It organizes reusable skills from coarse-grained task routing and planning down to workflow-level procedures and atomic analytical operations such as anomaly detection, dimensional drill-down, attribution, visualization, and evidence summarization.

Host makes the facts and methods executable. It reads skill specifications, references, and scripts, materializes them into a DAG execution graph, schedules independent branches in parallel, exposes tools for SQL, Python, file operations, and report construction, and records intermediate and final artifacts.

## End-to-End Walkthrough

The following walkthrough introduces the system through a typical example: **"analyze the average GAAP value of valid users for product X"**.

<p align="center">
  <img src="assets/use_case.png" alt="QwenPaw-Data end-to-end use case" width="900" />
</p>

**Plan.** Host consults Skill-Hub to select the relevant routing and planning skills, then decomposes the request into a DAG-shaped workflow: fetch data, detect anomalies, drill down by user type and region in parallel, conduct causal analysis, and generate the final report. DataBridge can already provide high-level hints that constrain which metrics, entities, and dimensions the plan should consider.

**Data retrieval.** DataBridge resolves business terms such as "valid users" through the Knowledge Graph and locates the GAAP metric through the Metadata Graph, linking the logical definition to physical tables and columns. Host invokes the corresponding data-access tools and registers the resulting dataset as an artifact.

**Analysis.** Host follows reusable Skill-Hub assets for anomaly detection, dimensional drill-down, contribution calculation, and causal analysis. During attribution, DataBridge supplies grounded evidence from knowledge and trace history so observed metric changes can be linked to business events, historical traces, or known rules.

**Report generation.** Host uses report-generation skills to organize findings, charts, methods, and source links into a decision-ready report. DataBridge supplies provenance and references so each headline conclusion remains connected to the metric definition, retrieved data, and analysis path that produced it.

**Self-evolution.** The task does not end when the report is delivered. Execution traces, user feedback, new analysis demands, and confirmed metric definitions become update signals for DataBridge and Skill-Hub, turning a completed task into reusable experience for the next similar analysis.

## What QwenPaw-Data Can Support

QwenPaw-Data is designed for serious analytical work that data teams handle day to day, including:

- **Business monitoring and anomaly diagnosis**: track core metrics such as DAU, revenue, or conversion, and locate which region, channel, or segment drives an unexpected change.
- **Trend and growth analysis**: analyze access, retention, or conversion trends, identify turning points, and attribute the changes behind them.
- **User and interaction insight**: mine dialogue and behavior logs to understand user intent, user needs, and how they evolve.
- **Periodic business reporting**: produce monthly or quarterly reports from data retrieval to a decision-ready document.
- **Ad-hoc deep-dive analysis**: answer open business questions with multi-dimensional decomposition and contribution attribution.

## Access Modes

QwenPaw-Data currently provides DataBridge as the local management surface. A standalone CLI host is available in the repository and continues to evolve as the primary execution entry point.

| Mode | Purpose | Typical Users | Runtime Form |
| --- | --- | --- | --- |
| **DataBridge UI** | Manage graph memory, semantic config, and related DataBridge assets | Analysts and platform operators | Local management UI backed by the DataBridge API. |
| **CLI** | Platform integration, secondary development, and local automation | Developers and platform teams | Lightweight command-line entry point for intent understanding, task planning, and workflow execution via `qwenpaw-data-cli`. |

## Repository Structure

QwenPaw-Data uses a Python + uv workspace monorepo.

The repository currently includes:

- **DataBridge**: manages metadata, business knowledge, trace history, graph memory, and its management interface.
- **Host Core / CLI**: shared host orchestration and the CLI execution entry point.
- **Data Analysis Skills**: reusable analytical skills and supporting assets.

```text
packages/                  # Python packages
docs/                      # architecture, release, and graph memory docs
scripts/                   # environment setup, startup, and end-to-end scripts
examples/                  # executable local demo data and deterministic smoke test
assets/                    # branding and documentation assets
```

## Quick Start: Clone to the First Data Task

### Installing from PyPI

The Python packages are published on PyPI:

```bash
pip install qwenpaw-data-cli        # `qwenpaw-data` command + host runtime
pip install qwenpaw-data-context    # DataBridge backend as a library
```

`pip install qwenpaw-data-cli` suits platform integrations that already run a
DataBridge service. The full local experience — DataBridge UI, demo data,
and managed services — uses the source checkout below.

### 0. Install local prerequisites

Windows 11, macOS, and Linux are supported. Install the following before continuing:

- Git
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- Node.js 22.22 or newer on the Node 22 LTS line, and npm
- Docker Desktop or Docker Engine with Docker Compose

DataBridge uses Python 3.12 by default. If it is not installed, `uv` can provision the interpreter during initialization. Start Docker Desktop/Engine before starting local services.
Windows users need PowerShell 7 and Docker Desktop configured for Linux
containers. Native Windows is covered by CI; WSL2 remains the recommended
fallback when a local Docker or networking setup is incompatible. See
[`docs/WINDOWS.md`](docs/WINDOWS.md).

### 1. Clone the repository and create local configuration

```bash
git clone https://github.com/agentscope-ai/QwenPaw-Data.git
cd QwenPaw-Data
cp .env.example .env
```

```powershell
git clone https://github.com/agentscope-ai/QwenPaw-Data.git
Set-Location QwenPaw-Data
Copy-Item .env.example .env
```

Keep all local settings in the root `.env`. Do not commit an `.env` that contains credentials.
Replace `NEO4J_PASSWORD=YOUR_PASSWORD` with a password for the local Neo4j instance.

### 2. Configure the package mirror and models

For faster downloads in mainland China, add a mirror to `.env`, for example:

```bash
UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple/
```

The Tsinghua mirror is also supported:

```bash
UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
```

Then configure the shared DataBridge and QwenPaw Data CLI model. The CLI uses these
OpenAI-compatible settings by default; the `QWENPAW_DATA_MODEL_*` variables are only
needed when the CLI should use a different model:

```bash
OPENAI_API_KEY=replace-with-your-api-key
OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_MODEL=qwen3.7-max
EMBED_MODEL=text-embedding-v3
EMBED_DIM=1024

# Optional CLI-specific overrides:
# QWENPAW_DATA_MODEL_PROVIDER=openai
# QWENPAW_DATA_MODEL_NAME=qwen3.7-max
# QWENPAW_DATA_MODEL_API_KEY=replace-with-your-api-key
# QWENPAW_DATA_MODEL_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
```

### 3. Initialize the local environment

```bash
# macOS / Linux
python3 scripts/init_local.py
```

```powershell
.\scripts\init_local.ps1
```

The initializer:

1. Creates `packages/qwenpaw-data-context/.venv`, installs the DataBridge backend, and builds the management UI.
2. Exports pinned versions and hashes from the committed `uv.lock`, installs them into the root `.venv` through the configured `UV_DEFAULT_INDEX`, and installs every workspace package in editable mode.
3. Imports the `databridge` MCP client into `${QWENPAW_DATA_HOME:-~/.qwenpaw-data}/host/workspace/.mcp` using a temporary source file.
4. Publishes the `qwenpaw-data` command without replacing an existing command:
   `${QWENPAW_DATA_CLI_BIN_DIR:-~/.local/bin}` on macOS/Linux, or
   `%LOCALAPPDATA%\QwenPaw Data\bin\qwenpaw_data.cmd` on Windows.

The mirror only controls downloads: initialization neither rewrites nor deletes `uv.lock`, and it does not leave a lockfile diff. Do not pass `--skip-build` on the first run; use `scripts/init_local.py --skip-build` (or the same option with the PowerShell wrapper) only when frontend build artifacts already exist.

If another system manages MCP configuration for the default workspace, pass `--skip-mcp-config` to the initializer.

On Windows, make the generated launcher available in the current PowerShell
terminal:

```powershell
$QwenPawDataBin = Join-Path $env:LOCALAPPDATA "QwenPaw Data\bin"
$env:Path = "$QwenPawDataBin;$env:Path"
Get-Command qwenpaw-data
```

See [the native Windows walkthrough](docs/WINDOWS.md) for the persistent user
`Path` option and a clean-machine checklist.

### 4. Start local services in terminal A

```bash
# macOS / Linux
python3 scripts/start_local.py
```

```powershell
.\scripts\start_local.ps1
```

This command starts local Neo4j, the DataBridge API, and the management UI, and remains attached to the terminal. It fails safely if DataBridge UI port `3000` or API port `8765` is already occupied; it does not terminate unrelated listeners.

Default URLs:

```text
DataBridge UI:   http://localhost:3000
DataBridge API:  http://localhost:8765
DataBridge Docs: http://localhost:8765/docs
```

### 5. Initialize the bundled demo in terminal B

The repository includes a deterministic PostgreSQL dataset, semantic workbook,
and Knowledge Graph document. Seed the data, import the semantic configuration,
and register the fixed datasource with:

```bash
# macOS / Linux
examples/init_demo.sh --register
```

```powershell
# Windows PowerShell
.\examples\init_demo.ps1 -Register
```

The command is repeatable and creates datasource `postgresql-demo-gaap` with
475 synthetic rows. It does not upload the Knowledge Graph document because
that step invokes the model configured in `.env`.

### 6. Build the demo graphs

Open `http://localhost:3000`:

1. In **Semantic Weaving**, select `Demo PG - GAAP use case` and submit a
   `FULL` weave.
2. In **KG Docs Management**, upload `examples/demo_kg_doc.docx`.
3. Wait until the document status is `ready`.

For a fast test without a real model API key, skip this graph-building step and
run the deterministic smoke test described below.

### 7. Verify the CLI

```bash
command -v qwenpaw-data
qwenpaw-data datasource list
```

```powershell
Get-Command qwenpaw-data
qwenpaw-data datasource list
```

The command should resolve to the launcher published by the initializer, and
`datasource list` should include `postgresql-demo-gaap`. The CLI loads the repository root `.env`
automatically; set `QWENPAW_DATA_ENV_FILE` only when a different dotenv file is
required.

### 8. Run a real data task

Use non-streaming mode for the first run so the complete execution summary is easy to inspect:

```bash
qwenpaw-data run \
  --no-stream \
  --datasource-id postgresql-demo-gaap \
  "Analyze the average GAAP value of valid users for product X during March 2026; show the trend over time and explain any spikes with relevant KG events"
```

```powershell
qwenpaw-data run --no-stream --datasource-id postgresql-demo-gaap "Analyze the average GAAP value of valid users for product X during March 2026; show the trend over time and explain any spikes with relevant KG events"
```

The expected peak is 2026-03-10, with an average GAAP value of approximately
`45.89`. Generated artifacts are stored under
`${QWENPAW_DATA_HOME:-~/.qwenpaw-data}/host/workspace` on macOS/Linux or
`$HOME\.qwenpaw-data\host\workspace` on Windows unless `QWENPAW_DATA_HOME` is set.

For a deterministic end-to-end test without a model API key:

```bash
# macOS / Linux
examples/init_demo.sh
uv run python examples/smoke_test.py
```

```powershell
# Windows PowerShell
.\examples\init_demo.ps1
uv run python .\examples\smoke_test.py
```

See [the full demo guide](examples/README.md) for direct SQL, reset, and
troubleshooting commands.

## Common Scripts

```bash
# Reuse existing frontend build artifacts
python3 scripts/init_local.py --skip-build

# Initialize without publishing a qwenpaw-data command link
python3 scripts/init_local.py --skip-cli-link

# Start DataBridge services, including the 3000 UI and 8765 API by default
scripts/init_databridge.sh
scripts/start_databridge.sh

# Start only the DataBridge API
scripts/start_databridge.sh --skip-frontend
```

On Windows, pass the same options to `init_local.ps1` or `start_local.ps1`.
The DataBridge-only `.sh` helpers in this block are macOS/Linux conveniences.

## Workspace Isolation

Agent tools run inside a workspace whose backend is selected per run
(`--workspace` flag or `QWENPAW_DATA_WORKSPACE` env, default `docker`):

| Backend | Isolation | Requirements | When to use |
|---------|-----------|--------------|-------------|
| `docker` (default) | Container-based workspace isolation: every tool (including Bash) executes inside a per-session container with only the task workspace mounted | a running Docker daemon | Normal execution; isolates tools from the host |
| `local` | Path containment plus AgentScope permission checks; shell commands that are approved still run with host-user privileges | explicit `--workspace local` | Trusted emergency/development use only |

```bash
# Check the environment first (Docker daemon, Neo4j, DataBridge API)
qwenpaw-data doctor

# Docker is the default
qwenpaw-data run --datasource-id "postgresql-xxxxxxxx" "..."

# Explicit unsandboxed host execution (trusted development only)
qwenpaw-data run --workspace local "..."

# Second explicit opt-in: disable prompts/denials even for local execution
qwenpaw-data run --workspace local --permission-mode bypass "..."
```

The permission policy defaults to `auto` (override with `--permission-mode` or
`QWENPAW_DATA_PERMISSION_MODE`). Docker uses `bypass` because the per-session
container is the execution boundary. An interactive local CLI uses
`accept_edits`: file edits inside the task workspace are allowed and riskier
calls require terminal confirmation. Unattended local execution uses
`dont_ask`, which denies calls that would otherwise require confirmation.
Subagents cannot prompt independently and fail closed on such requests.

Notes for the `docker` backend:

- The container image is built on first use (`python:3.11-slim` + the
  analysis stack: pandas, numpy, matplotlib, openpyxl). Customize via
  `QWENPAW_DATA_DOCKER_BASE_IMAGE` / `QWENPAW_DATA_DOCKER_EXTRA_PIP`.
- Containers reach host services (DataBridge) through
  `host.docker.internal` (override with `QWENPAW_DATA_DOCKER_HOST_ALIAS`).
  Containers are stopped and removed when the run finishes.
- Docker commands run in dedicated process groups. Timeout and cancellation
  trigger TERM/KILL cleanup; if cleanup itself fails, QwenPaw Data closes the
  workspace container rather than leaving an unknown process running.
- On macOS, [colima](https://github.com/abiosoft/colima) provides an
  unattended, license-free Docker daemon:
  `brew install colima docker && colima start`.
- Current boundary is the container itself: resource limits
  (CPU/memory/pids), egress network policy, and non-root execution are
  not yet applied — they are on the roadmap. Do not treat the `docker`
  backend as a hardened sandbox.

## Security Model and Known Limitations

QwenPaw-Data is designed for **local-first, single-user deployments**. Read this
section before exposing any service beyond `127.0.0.1`.

- **Network**: all services bind to `127.0.0.1` by default. Exposing them
  (`--host 0.0.0.0`, `FRONTEND_HOST`, `QWENPAW_DATA_MCP_HOST`) is an explicit
  opt-in; configure `QWENPAW_DATA_API_TOKEN` or scoped `QWENPAW_DATA_API_KEYS` first so
  the DataBridge REST and HTTP MCP endpoints require a `Bearer` token, and
  restrict `QWENPAW_DATA_CORS_ORIGINS` to trusted origins.
- **Execution**: the Host runs agent tools in a per-run **Docker** workspace
  by default. The explicit `--workspace local` escape hatch executes shell
  commands on your machine with your user's privileges and is *not*
  sandboxed. File/search tools remain path-contained, and timed-out or
  cancelled commands have their whole process group terminated in both local
  and Docker workspaces. Local Bash remains capable of arbitrary host-user
  actions after permission approval. Permission policy is workspace-aware:
  Docker defaults to `bypass` within its container boundary; interactive local
  runs use `accept_edits` with terminal confirmation, and unattended local
  runs fail closed with `dont_ask`. This does not make the Docker backend a
  hardened sandbox; its resource and network limits remain as stated above.
- **Authorization**: scoped API keys separate `query`, `write`, `manage`, and
  `credentials:manage`. `QWENPAW_DATA_API_TOKEN` remains a full-scope compatibility
  key. For scoped clients, set `QWENPAW_DATA_CLIENT_API_TOKEN`; every API route is
  fail-closed until explicitly classified. This is API-key authorization, not
  a multi-user identity/RBAC system.
- **Browser and abuse controls**: unsafe browser requests are checked against
  the exact CORS/Origin allowlist and Fetch Metadata. Authentication failures
  enter a per-client penalty box; authenticated requests use per-principal,
  per-scope token buckets. Privileged operations and Host, authentication,
  authorization, CSRF, and rate-limit denials are written as body-free JSON
  records to `security_audit.jsonl`. Forwarded client IPs are trusted only when
  `QWENPAW_DATA_TRUSTED_PROXIES` is explicitly configured.
  The same edge controls apply to standalone HTTP MCP mode.
  Throttle state is process-local; multi-worker or horizontally scaled
  deployments need a shared limiter at the gateway or application layer.
- **Scope**: import task status and preview/confirm plans use a process-safe
  SQLite job store with TTL, idempotency, leases, retry bounds, and restart
  recovery. Interrupted in-process work is marked failed after restart rather
  than silently disappearing; automatic resumption is only implemented for
  embedding rebuild jobs. Native Windows support and its validation boundary
  are documented in `docs/WINDOWS.md`. See `SECURITY.md` for how to report
  vulnerabilities.

To store only a token digest in `QWENPAW_DATA_API_KEYS`, generate it with:

```bash
printf %s 'your-long-random-token' | shasum -a 256
```

Operational and release references:

- [Resource governance](./docs/RESOURCE_GOVERNANCE.md)
- [Compatibility policy](./docs/COMPATIBILITY.md)
- [Release process and public-history boundary](./docs/RELEASING.md)
- [Security policy](./SECURITY.md)
- [Support and version policy](./SUPPORT.md)
- [Changelog](./CHANGELOG.md)

## License

QwenPaw-Data is licensed under the [Apache License 2.0](./LICENSE).
See [NOTICE](./NOTICE) for third-party attributions and
[CONTRIBUTING.md](./CONTRIBUTING.md) for how to contribute.
