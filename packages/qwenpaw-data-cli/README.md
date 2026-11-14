# qwenpaw-data-cli

Standalone command-line interface for QwenPaw Data.

This package provides the `qwenpaw-data` command for datasource administration,
semantic configuration, deployment diagnostics, and remote Host conversations.
The [Console](../qwenpaw-data-console/README.md) remains the full interactive UI.

The command automatically loads `QWENPAW_DATA_ENV_FILE` when set. Otherwise, an
editable checkout is discovered from the CLI package location and the
repository root `.env` is loaded. Existing process environment variables take
precedence over dotenv values. Outside an editable checkout, the current
working directory is used as the fallback configuration root.

Available command groups:

- `datasource`
- `semantic`
- `doctor`
- `run` / `chat` (explicit Host-service connection)
- `plan` (prompt-only SOP generation through Host, without execution)

`run` and `chat` now use the current Host API, not the legacy in-process DAG
runtime. Select an already-running Host explicitly:

```bash
qwenpaw-data run --host-url http://127.0.0.1:8000 "Summarize sales"
qwenpaw-data chat --host-url http://127.0.0.1:8000
qwenpaw-data run --host-url http://127.0.0.1:8000 --session-id <id> --no-stream "Continue"
```

`QWENPAW_DATA_HOST_URL` can supply the origin; `QWENPAW_DATA_HOST_TOKEN` supplies
the Host API token or user JWT without putting it in command-line arguments.
Use HTTPS for remote hosts. The client does not follow redirects or inherit HTTP
proxy settings. It does not forward DataBridge credentials or caller-selected
user identities. Model, sandbox and permission policies belong to the server;
the old `--workspace` / `--permission-mode` flags are not supported in this mode.

Session IDs are printed to stderr and retained on the server for continuation.
New CLI sessions disable clarification; an existing session requiring it must
be continued in Console. Tool approval prompts default to deny, and non-TTY
input always denies. Ctrl+C or an interrupted event stream requests cancellation
of the admitted turn; a disconnected stream is never reported as success.
If admission itself loses its response, the client cannot know the new chat ID;
inspect the session in Console before retrying to avoid duplicate work.

`plan` returns the OSS SOP YAML contract (`name`, `description`,
`expected_outcome`, and `nodes` with stable IDs and acyclic `deps`):

```bash
qwenpaw-data plan --host-url http://127.0.0.1:8000 "Plan a sales analysis" -o sales.yaml
```

Planning uses the authorized session's model preferences, but does not start an
agent, allocate an execution workspace, query a datasource, or run tools. Supply
schema/facts in the prompt if needed; otherwise the generated plan must include
discovery steps. The datasource ID is context, not proof of schema inspection.
`--output` exclusively creates a new file and refuses to overwrite existing data.
This does not restore the legacy tool-assisted planner or the strict DAG executor.

**Remaining compatibility gaps:** `execute` and the old in-process
`QwenPawDataHost` interface are not restored. Remote `run` is a normal agent turn,
not a claim of legacy plan-and-execute SOP equivalence.

Run `qwenpaw-data doctor` before the first task to check supported Python/Node
versions, uv, model and authentication configuration, QwenPaw Data Home, DataBridge
MCP, Docker, Neo4j, and the DataBridge API. `qwenpaw-data doctor --json` emits a
machine-readable report that reports whether credentials are set without
printing their values.

`datasource` manages the datasources configured in DataBridge:

```bash
qwenpaw-data datasource list
qwenpaw-data datasource get <datasource_id> [--show-config]
qwenpaw-data datasource create --name <n> --type <t> \
  (--config-file <f.json> | --config '<json>') [--test]
qwenpaw-data datasource update <datasource_id> [--name <n>] [--type <t>] [--config-file <f>]
qwenpaw-data datasource delete <datasource_id> [--yes]
qwenpaw-data datasource test (<datasource_id> | --type <t> --config-file <f>)
```

Set `QWENPAW_DATA_CM_BASE_URL` to the DataBridge origin. It defaults to
`http://127.0.0.1:8765`. Every command emits JSON and masks saved passwords,
AccessKeys, and STS tokens before writing anything to stdout; there is no
option that prints stored credentials. `list` uses the credential-free
discovery endpoint, while the management subcommands call the
`/api/semantic-config` routes and need an API key with the
`credentials:manage` scope when scoped keys are configured
(`QWENPAW_DATA_CLIENT_API_TOKEN` or `QWENPAW_DATA_API_TOKEN`).

`semantic` manages the DataBridge semantic configuration layer. The seven
resources `domain`, `dataset`, `column`, `dimension`, `binding`, `metric`,
and `formula` share the same verbs:

```bash
qwenpaw-data semantic <resource> list [filters] [--page N --size N | --all]
qwenpaw-data semantic <resource> get <id>
qwenpaw-data semantic <resource> create (field flags | --file payload.json)
qwenpaw-data semantic <resource> update <id> (field flags | --file payload.json)
qwenpaw-data semantic <resource> delete <id> [--yes]
```

`binding` and `formula` also support dataset-level batch deletion with
`delete --dataset-id <id>`. Reads require the `query` scope and writes the
`manage` scope. Two more subcommands cover import and publishing:

```bash
# Import a semantic configuration workbook into the draft store
qwenpaw-data semantic import --file demo_semantic_config.xlsx

# Publish (weave) the configuration into the graph store
qwenpaw-data semantic weave submit --datasource-id <id> [--mode FULL] [--wait]
qwenpaw-data semantic weave list [--datasource-name <n>] [--task-name <n>]
qwenpaw-data semantic weave kill <task_id>
```

With `--wait` the CLI polls until the task reaches a terminal state
(`success`, `failed`, `killed`), printing progress to stderr and the final
task record as JSON to stdout; the exit code is `0` only for `success`.

Remote agent commands use the models configured in Host preferences. The CLI
does not forward local model credentials or override the server's model selection.

## Output and logs

The CLI writes streamed agent events, final answers, and execution summaries to
stdout. Interactive prompts and concise command errors remain explicit CLI
output on the terminal rather than raw log records. Python logging is written
to `${QWENPAW_DATA_HOME}/host/qwenpaw_data.log` at `INFO` level and above;
`QWENPAW_DATA_HOME` defaults to `~/.qwenpaw-data`.

The log rotates at 50 MB and keeps two backups as `qwenpaw_data.log.1` and
`qwenpaw_data.log.2`. Rotation assumes one active CLI process per `QWENPAW_DATA_HOME`.
Direct users of `qwenpaw-data-host-core` continue to own their process logging
configuration.

This package intentionally omits `serve` and local initialization helpers from
the public CLI help.
