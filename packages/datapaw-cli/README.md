# datapaw-cli

Standalone command-line interface for DataPaw.

This package provides the `datapaw` command and connects local host workflows through `datapaw-host-core`.

The command automatically loads `DATAPAW_ENV_FILE` when set. Otherwise, an
editable checkout is discovered from the CLI package location and the
repository root `.env` is loaded. Existing process environment variables take
precedence over dotenv values. Outside an editable checkout, the current
working directory is used as the fallback configuration root.

Available command groups:

- `plan`
- `execute`
- `run`
- `chat`
- `datasource`
- `semantic`
- `doctor`

Run `datapaw doctor` before the first task to check supported Python/Node
versions, uv, model and authentication configuration, DataPaw Home, DataBridge
MCP, Docker, Neo4j, and the DataBridge API. `datapaw doctor --json` emits a
machine-readable report that reports whether credentials are set without
printing their values.

`datasource` manages the datasources configured in DataBridge:

```bash
datapaw datasource list
datapaw datasource get <datasource_id> [--show-config]
datapaw datasource create --name <n> --type <t> \
  (--config-file <f.json> | --config '<json>') [--test]
datapaw datasource update <datasource_id> [--name <n>] [--type <t>] [--config-file <f>]
datapaw datasource delete <datasource_id> [--yes]
datapaw datasource test (<datasource_id> | --type <t> --config-file <f>)
```

Set `DATAPAW_CM_BASE_URL` to the DataBridge origin. It defaults to
`http://127.0.0.1:8765`. Every command emits JSON and masks saved passwords,
AccessKeys, and STS tokens before writing anything to stdout; there is no
option that prints stored credentials. `list` uses the credential-free
discovery endpoint, while the management subcommands call the
`/api/semantic-config` routes and need an API key with the
`credentials:manage` scope when scoped keys are configured
(`DATAPAW_CLIENT_API_TOKEN` or `DATAPAW_API_TOKEN`).

`semantic` manages the DataBridge semantic configuration layer. The seven
resources `domain`, `dataset`, `column`, `dimension`, `binding`, `metric`,
and `formula` share the same verbs:

```bash
datapaw semantic <resource> list [filters] [--page N --size N | --all]
datapaw semantic <resource> get <id>
datapaw semantic <resource> create (field flags | --file payload.json)
datapaw semantic <resource> update <id> (field flags | --file payload.json)
datapaw semantic <resource> delete <id> [--yes]
```

`binding` and `formula` also support dataset-level batch deletion with
`delete --dataset-id <id>`. Reads require the `query` scope and writes the
`manage` scope. Two more subcommands cover import and publishing:

```bash
# Import a semantic configuration workbook into the draft store
datapaw semantic import --file demo_semantic_config.xlsx

# Publish (weave) the configuration into the graph store
datapaw semantic weave submit --datasource-id <id> [--mode FULL] [--wait]
datapaw semantic weave list [--datasource-name <n>] [--task-name <n>]
datapaw semantic weave kill <task_id>
```

With `--wait` the CLI polls until the task reaches a terminal state
(`success`, `failed`, `killed`), printing progress to stderr and the final
task record as JSON to stdout; the exit code is `0` only for `success`.

The default CLI model uses provider `openai` together with `LLM_MODEL`,
`OPENAI_API_KEY`, and `OPENAI_BASE_URL`. Set the corresponding
`DATAPAW_MODEL_PROVIDER`, `DATAPAW_MODEL_NAME`, `DATAPAW_MODEL_API_KEY`, and
`DATAPAW_MODEL_BASE_URL` variables to override individual values for the CLI.

## Output and logs

The CLI writes streamed agent events, final answers, and execution summaries to
stdout. Interactive prompts and concise command errors remain explicit CLI
output on the terminal rather than raw log records. Python logging is written
to `${DATAPAW_HOME}/host/datapaw.log` at `INFO` level and above;
`DATAPAW_HOME` defaults to `~/.datapaw`.

The log rotates at 50 MB and keeps two backups as `datapaw.log.1` and
`datapaw.log.2`. Rotation assumes one active CLI process per `DATAPAW_HOME`.
Direct users of `datapaw-host-core` continue to own their process logging
configuration.

This package intentionally omits `serve` and local initialization helpers from
the public CLI help.
