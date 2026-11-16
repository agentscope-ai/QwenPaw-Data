# Native Windows walkthrough

The native PowerShell entry points initialize the workspace packages and start
the DataBridge API and its frontend. They do not start the current Auth and
Host services or perform the multi-user chat setup provided by `start.sh` and
`setup_chat.sh`. The complete native Windows workflow is not yet validated;
use the WSL2 workflow for the current multi-service product.

This walkthrough describes the narrower native DataBridge entry points and
their prerequisites. They require Windows 11, PowerShell 7 and Docker Desktop
with Linux containers. A successful initializer alone does not establish that
the current Context service has its Auth/Host-owned database tables or a ready
graph workspace.

## 1. Install and verify prerequisites

Install:

- Windows 11 and PowerShell 7 (`pwsh`)
- Git for Windows
- Python 3.11 through 3.13
- [uv for Windows](https://docs.astral.sh/uv/getting-started/installation/)
- Node.js 22.22 or newer on the Node 22 LTS line
- Docker Desktop with Linux containers and Docker Compose v2

Start Docker Desktop, then open a new PowerShell 7 terminal and verify:

```powershell
pwsh --version
git --version
py -3 --version
uv --version
node --version
npm --version
docker version
docker compose version
```

QwenPaw Data does not install or elevate privileges to start Docker Desktop.

## 2. Clone and configure

```powershell
git clone https://github.com/agentscope-ai/QwenPaw-Data.git
Set-Location QwenPaw-Data
Copy-Item .env.example .env
notepad .env
```

Set a real `OPENAI_API_KEY`, the matching OpenAI-compatible base URL and model
names. The initializer automatically replaces the placeholder Neo4j password
with a random local password when necessary.

Keep the checkout on a local NTFS path such as
`C:\Users\you\src\QwenPaw-Data`. Avoid network drives and synchronized
folders while validating the first run.

## 3. Initialize

```powershell
.\scripts\init_local.ps1
```

The initializer creates the Python environments, installs all workspace
packages, builds the frontend, imports the DataBridge MCP definition, and
creates:

```text
%LOCALAPPDATA%\QwenPaw Data\bin\qwenpaw_data.cmd
```

Make the launcher available in the current terminal:

```powershell
$QwenPawDataBin = Join-Path $env:LOCALAPPDATA "QwenPaw Data\bin"
$env:Path = "$QwenPawDataBin;$env:Path"
Get-Command qwenpaw-data
```

To persist it for future terminals, add that directory to the user `Path`
through **System Properties → Environment Variables**. The initializer does not
silently change the user registry.

## 4. Start services

In terminal A:

```powershell
.\scripts\start_local.ps1
```

Leave this terminal open. The command starts Neo4j through Docker Compose,
DataBridge API on `http://127.0.0.1:8765`, and the UI on
`http://127.0.0.1:3000`. It refuses to terminate unknown processes already
using ports 3000 or 8765.

In terminal B:

```powershell
$QwenPawDataBin = Join-Path $env:LOCALAPPDATA "QwenPaw Data\bin"
$env:Path = "$QwenPawDataBin;$env:Path"
qwenpaw-data doctor
```

Do not continue until Docker, Neo4j, and DataBridge are reported healthy.
The current Context service depends on the shared Auth/Host schema and graph
workspace provisioning; these native entry points do not prepare those owners.
On a fresh installation, use the WSL2 full-service workflow instead.

## 5. Run the deterministic demo

The bundled smoke test uses a local model stub, but exercises real Host and
DataBridge processes, native MCP SQL, PostgreSQL, and SQL chat persistence.
Run it only after provisioning the full service stack and PostgreSQL; the native
DataBridge-only launcher above is not sufficient.

```powershell
.\examples\init_demo.ps1
uv run python .\examples\smoke_test.py
```

The test must finish with:

```text
QwenPaw Data deterministic demo smoke passed.
```

If the complete stack is already running and configured, register the demo
datasource (the initializer prints its ID):

```powershell
.\examples\init_demo.ps1 -Register
qwenpaw-data datasource list
```

Open the local Console, configure its model, select `Demo PG - GAAP use case`,
and submit: “Analyze the average GAAP value of valid users for product X during
March 2026; show the trend over time and explain any spikes with relevant KG
events.” See [Console startup](../packages/qwenpaw-data-console/README.md).
The former `qwenpaw-data run` command is retired in 0.4.

The expected peak is 2026-03-10, with an average GAAP value of approximately
`45.89`. To relate the spike to Knowledge Graph events, use the DataBridge UI
to run a `FULL` semantic weave and upload `examples\demo_kg_doc.docx`.

Generated host artifacts are stored below
`$HOME\.qwenpaw-data\host\workspace\artifacts` unless `QWENPAW_DATA_HOME` overrides
the root.

## 6. Stop or reset

Press Ctrl+C in terminal A to stop DataBridge processes. Stop the demo
PostgreSQL container without deleting its data:

```powershell
docker compose -f .\examples\docker-compose.yml down
```

Delete the demo volume as well:

```powershell
docker compose -f .\examples\docker-compose.yml down --volumes
```

## Workspace behavior

The default `docker` workspace runs agent tools inside a Linux container and
requires Docker Desktop. Containers access the host DataBridge through
`host.docker.internal`.

The explicit `--workspace local` option delegates shell execution to
AgentScope's native PowerShell workspace. It executes with the current Windows
user's privileges and is an unsafe development escape hatch, not a sandbox.

## WSL2 fallback

Enable Docker Desktop integration for an Ubuntu WSL2 distribution, clone the
repository inside the Linux filesystem (for example `~/src/QwenPaw-Data`, not
`/mnt/c`), and follow the macOS/Linux commands in the main README.

## Validation boundary

Windows CI runs the real native initializer and Python tests, and defines checks
for the PowerShell lifecycle, API/UI health, CLI and frontend. The current
Python failures prevent the later native lifecycle smoke from running, so they
are not evidence that native startup has passed. GitHub's Windows runner does
not provide a Linux Docker Desktop daemon, so Neo4j/PostgreSQL and Docker-workspace end-to-end
validation must also pass on a clean Windows 11 machine before a release is
advertised as Windows-validated.
