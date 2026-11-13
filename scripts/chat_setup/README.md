# Multi-user chat setup

This opt-in workflow creates or reuses accounts and workspaces through Auth APIs,
configures user models, optionally imports semantic data, and verifies a real
chat. It requires `QWENPAW_DATA_AUTH_MODE=multi-user`, `curl`, `jq`, configured
model credentials, and the shared Auth/Context/Host services. For the default
OSS no-login workflow, use `scripts/init_local.py` and `scripts/start_local.py`.

Run `bash scripts/setup_chat.sh`. It starts missing services or waits for an
existing launcher's readiness handoff. A setup failure returns nonzero and
stops only services owned by that invocation; existing data and services remain.

Configuration uses `.env` (or `QWENPAW_DATA_ENV_FILE`) plus an optional sibling
`<env-file>.chat-setup.json`. [The example](fixtures/config.example.json) describes
the account matrix and model/data settings. Development defaults contain only
synthetic Acme accounts. Production requires an explicit account matrix.

Datasource `host` and `workbook` are empty by default. Leave them empty to skip
connection creation/import/weaving. To import data, export a workbook template
from the current Context UI, fill it with your data and set its path explicitly.
Private Cloud workbooks are not redistributed. Hologres and BigQuery remain
optional connectors, not startup prerequisites.

Generated passwords and diagnostics are stored under `<env-file>.chat-setup/`
with restrictive permissions. Cookie jars use a private temporary directory and
are removed on completion or failure. These files and service manifests are
ignored by Git. Do not copy them into a report or commit.

The Python verifier checks identity, model readiness and a completed visible
answer. A mocked test run is not proof of live model/provider readiness; running
setup with real credentials can incur provider charges.
