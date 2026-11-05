# QwenPaw-Data Console

Reusable local browser portal for the official Host HTTP/SSE service and
DataBridge. Wheels include the prebuilt chat UI and DataBridge management UI;
users do not need Node.js, an npm build, or a copied frontend.

```sh
pip install qwenpaw-data-console
# Set QWENPAW_DATA_API_TOKEN to the running Host's token.
qwenpaw-data-console --host-url http://127.0.0.1:8766 \
  --bridge-url http://127.0.0.1:8765 --port 8767
```

Open `http://127.0.0.1:8767/console`. DataBridge is at `/databridge/`.
Host, DataBridge and Neo4j retain their own lifecycles. Omit `--bridge-url`
to run only the chat portal. `QWENPAW_DATA_CONTEXT_API_TOKEN` can supply a
separate DataBridge token. The portal binds to loopback and keeps service
credentials on the server; it is intended for local research, not multiuser hosting.

## Domain applications

```python
from qwenpaw_data.console import ConsoleConfig, create_app

app = create_app(
    host_url="http://127.0.0.1:18767",
    token=host_token,
    bridge_url="http://127.0.0.1:18766",
    config=ConsoleConfig(
        title="My research platform",
        assistant_name="Research Analyst",
        welcome_description="Ask a question about your research data.",
        workspace_name="Research",
    ),
)
```

The CLI also accepts these public settings as JSON via `--config`.
Branding is applied at runtime; the same wheel serves different domains.
Domain routes can be registered before the final SPA fallback with
`create_app(..., routers=[router])`. Skills, datasource setup and database
reconstruction belong to the consuming application. API/model keys are
not presentation settings and must never be placed in ConsoleConfig.

The Host contract is `/api/v1` with replayable SSE, sessions, model settings,
artifacts and scheduled jobs. Skills/MCP/channel management pages remain
disabled because Host 0.3.3 does not expose their management APIs.
The two UIs have separate dependency trees (React 18 chat / React 19
DataBridge); the portal serves both from one origin.

## Development and releases

From the monorepo root:

```sh
python scripts/build_console.py
uv build --package qwenpaw-data-console
uv run pytest packages/qwenpaw-data-console/tests
npm test --prefix packages/qwenpaw-data-console/frontend
```

`npm ci` uses each application's lockfile. The build excludes private
environment configuration. Both wheel and sdist carry the built assets;
wheel builds fail if either UI is absent. Rebuilding a release sdist does
not require Node.js. Editable source installs require the asset build once.

The pinned Chat SDK's `@ant-design/x` dependency declares an Ant Design 6
peer while `@agentscope-ai/design` requires Ant Design 5. npm reports this
upstream peer mismatch; the existing UI remains on Ant Design 5. CI builds
and tests that locked tree, audits all dependencies at the high threshold,
and generates its installed-package SBOM with `--legacy-peer-deps` for
inventory only (recorded in the SBOM metadata). Installs and audits do not
use that override. A coordinated SDK/design upgrade should resolve the peer
mismatch before claiming support for Ant Design 6.

Chat frontend provenance: QwenPaw-Geo frontend at
`ff69ce72abf2424fc14883e911c3bc6a9d8fa53e`, adapted in QwenPaw-Econ,
then generalized here. DataBridge UI is built directly from the official
`qwenpaw-data-context/frontend` sources, using the supported `console` mode.
