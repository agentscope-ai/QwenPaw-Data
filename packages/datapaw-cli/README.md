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
- `datasource list`
- `doctor`

Run `datapaw doctor` before the first task to check supported Python/Node
versions, uv, model and authentication configuration, DataPaw Home, DataBridge
MCP, Docker, Neo4j, and the DataBridge API. `datapaw doctor --json` emits a
machine-readable report that reports whether credentials are set without
printing their values.

`datasource list` reads every datasource configured in DataBridge:

```bash
datapaw datasource list
```

Set `DATAPAW_CM_BASE_URL` to the DataBridge origin. It defaults to
`http://127.0.0.1:8765`. The command emits JSON and masks saved passwords,
AccessKeys, and STS tokens before writing anything to stdout.

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
