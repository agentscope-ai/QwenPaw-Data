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
- `execute` (client-scheduled SOP DAG execution through Host)
- `migration` (offline JSON inspection, verified backup and read-only preparation)

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
This does not restore the legacy tool-assisted planner.

Execute a validated SOP using a deterministic dependency scheduler:

```bash
qwenpaw-data execute --host-url http://127.0.0.1:8000 sales.yaml
```

The client starts one Host turn per node, sequentially in topological order.
A successor cannot start until all predecessors have successful terminal
responses. Failed, cancelled or truncated turns abort execution; Ctrl+C stops
the active turn. Node IDs and the session ID are printed to stderr. Tool approval
uses the same deny-by-default handling as `run`. Validate malformed/cyclic SOPs
before any session creation. A new dedicated session is the default;
`--session-id` uses that session's existing datasource and rejects an accompanying
`--datasource-id` override.

This schedules turns, not every tool action within them; a successful terminal
response is not independent verification of the business outcome. Completed
turns remain in Host history, but DAG checkpoint/resume, automatic retries,
parallel execution and rollback of tool side effects are not implemented.
Rerunning the file executes every node again; inspect history before retrying.

**Remaining compatibility gaps:** the old in-process
`QwenPawDataHost` interface is not restored. Remote `run` is a normal agent turn,
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

## Find a pre-upgrade local SQLite store

`qwenpaw-data migration discover` checks the current runtime home, or pass
`--source-home /absolute/old-home`. It reports known candidate paths (including
the v0.3 Host default `host/host.db`) alongside the current local default
`control_plane.db`, even when both exist. It does not open either database,
search arbitrary directories, inspect rows, or claim that an import has run.
With an explicit DB URL, the reported default is not necessarily your active DB.

For now, stop the old writers and preserve the **whole old home**, including
SQLite sidecars, workspace files and encryption keys. To access old records,
use the matching older release on a backup. Do not rename the old database or
point the new Host at it: old owner IDs and credentials need an explicit
conversion. The JSON workflow below is not a SQLite importer.

### Requirements for a future SQLite importer

Only discovery is implemented for the `oss-host-sqlite-v0.3` layout. New layouts
get their own ID; do not change the meaning of an existing one. Before shipping
a conversion for any layout, the [OSS adapter decision](../../docs/adr/0001-internal-baseline-and-oss-adapters.md)
requires this sequence:

1. Stop source and target writers. Verify recoverable backups and retain any externally supplied encryption keys separately.
2. Dry-run against a copy. Bind the plan to the source, target and layout ID; show the mapping from old owners to users, tenants and workspaces, and report unreadable credentials or unmapped records. Do not print secrets.
3. Require explicit approval of that plan. Import into the specified target without rewriting the source or running historical cron jobs. Track progress so a restart cannot duplicate records.
4. Verify through the new Host after restart: read sessions and files as the intended owner, check masked model settings and confirm job state. Demonstrate recovery or rollback on a copy before offering the importer. A target file's existence is not a migration receipt.

## Legacy JSON migration

`migration inspect`, `backup`, `plan`, `provision-key`, `prepare`, `apply`,
`recover`, `check-rollback` and `rollback` provide an explicit offline workflow.
Writes currently require POSIX, an initialized SQLite/PostgreSQL target and all
application writers stopped. Use separate source, backup and target homes;
source data is never overwritten. Begin with read-only preparation:

```bash
qwenpaw-data migration inspect --source-home /absolute/old-home --identity-map identities.json
qwenpaw-data migration backup --source-home /absolute/old-home --destination /absolute/backup --source-stopped
qwenpaw-data migration plan --source-home /absolute/old-home --backup /absolute/backup \
  --target-home /absolute/new-home --identity-map identities.json --source-stopped \
  --target-key-env MIGRATION_TARGET_KEY --source-key-env MIGRATION_SOURCE_KEY \
  --context-trigger-ratio 0.8 --context-reserve-ratio 0.1
```

Set the named key variables securely before invoking the command; arguments are
variable **names**, never secret values. The target key is 32 bytes encoded as
hex; the old key accepts at least 32 bytes. Omit `--source-key-env` only if old
credentials are plaintext. Keep both keys separately: backups do not capture
environment secrets, and planning does not provision the target master key.
Use the actual target runtime ratio defaults, not necessarily the example values.

Identity mapping format:

```json
{"version":1,"users":{"old-user":{"user_id":"new-user","tenant_id":"tenant","workspace_id":"workspace"}}}
```

For extra models missing context size, supply `--model-context-sizes models.json`
with `{"version":1,"users":{"old-user":{"provider/model":12345}}}`. Values must
be correct for the selected model; unknown or unused additions are rejected.
`--disable-cron` explicitly consents to **propose** imported jobs as disabled.
Planning never runs them. Removed catalog models, old DAG data, agent state and
unassigned shared workspace files remain explicit blockers. Known CLI logs and
recognized empty lock files are retained only in the verified backup and listed
as notices; unknown/nonempty lock files require explicit disposition.

Optionally add `--target-db-env MIGRATION_TARGET_DB --target-stopped` to `plan`
after stopping target writers. Set that variable securely to the **already
initialized target control-plane/Host database** URL. No ambient database setting
is used. Supported URLs are file `sqlite:////absolute/target.sqlite` (no query
options) or synchronous `postgresql+psycopg://.../database` (the existing
`qwenpaw-data-auth[postgres]` optional driver; host, user and database must be
explicit). The check validates
required columns/primary keys, active users/tenants/workspaces and native Auth
roles, then checks all prepared primary keys for existing rows. Conflicts block
even if values are identical. Preference-only users also require authorization.
It does not create accounts, tables, files, keys, or overwrite/merge rows.

SQLite must be an existing regular file, with no `-wal`, `-shm` or `-journal`
sidecars; checkpoint/close it through its owning application first. The checker
never deletes or recovers journals. Its [immutable read-only connection](https://www.sqlite.org/uri.html)
requires the stopped-writer acknowledgement and checks for file changes.
PostgreSQL checks use a [read-only repeatable-read transaction](https://www.postgresql.org/docs/current/sql-set-transaction.html)
with query timeouts and rollback; this is not a promise of zero server-side disk
activity. PostgreSQL-compatible/Hologres services are not yet accepted as tested
targets. The selected database must be the one the target deployment will use.

Exit `0` means conversion and any requested target checks passed, `2` means there
are reported blockers, and `1` means the command failed (including unavailable
or incompatible target schema). Even with `conversion_complete: true` and
`target_validation.target_checks_passed: true`, the plan reports `import_ready:
false`: this read-only command does not save an executable plan or reserve the
target. Use the explicit saved-plan workflow below; import revalidates mutable state.
Reports contain counts/paths and safe errors, not source payloads or credentials.

### Explicit local master-key configuration

For a **local key-file deployment on POSIX**, stop target writers and explicitly
configure the selected key in an existing target home:

```bash
qwenpaw-data migration provision-key --target-home /absolute/new-home \
  --target-key-env MIGRATION_TARGET_KEY --target-db-env MIGRATION_TARGET_DB \
  --target-stopped
```

Unlike `plan`, this command **writes a private `preferences.key` file**. It first
authenticates all existing encrypted provider credentials in the explicitly
selected target database, including other users, using that key. Malformed,
wrong-key or oversized credentials and missing schema block publication; existing
plaintext credentials are counted, not rewritten. The current scan limit is
100,000 records and 64 KiB per credential. SQLite/PostgreSQL selection and stopped
writer requirements are the same as for target preflight.

The home must exist, be owned by the current user and not be group/world writable.
The command publishes a complete, synced, mode-0600 key without replacement.
An identical private existing key is verified without modification; different,
malformed, public, symlinked or hard-linked keys are refused, not repaired. On an
interrupted operation, inspect the target before retrying; an already-published
key is retained. Keep a separate secure backup of this key.

This does **not** configure deployment environment variables or verify that other
services use this home/key. Host runtime variables
`QWENPAW_DATA_PREFERENCES_MASTER_SECRET` / `QWENPAW_DATA_PREFS_MASTER_SECRET` take
precedence over the local file; production requires environment provisioning.
Configure those secrets consistently outside the CLI for every service sharing
the database. Native Windows ACL acceptance and automatic deployment-secret
provisioning are not implemented. No SQL rows are written, and `import_ready`
remains false.

### Save, apply and recover the exact plan

Initialize accounts, memberships and the current Host schema separately, including
`legacy_import_targets`, `legacy_import_runs` and `legacy_import_rollbacks`.
Migration never creates accounts, repairs schema or changes the runtime database
selection. Stop **all** target writers, including services on other machines.
Provision the target key first. `prepare`/`apply`/`recover` check Host environment
key precedence or the already provisioned key in the explicit target home, without
generating a key. `--deployment-config-confirmed` additionally acknowledges that
every service will use the selected home, database and key; the CLI cannot attest
another process's environment. Production requires an environment-provisioned key.

Create a private mode-0700 plan directory outside source, backup and target. Choose
one unique lowercase 32-hex-character run ID and retain it. Example Bash workflow
(named key/DB variables must already be securely configured):

```bash
MIGRATION_RUN_ID="$(python -c 'import uuid; print(uuid.uuid4().hex)')"
migration_target=(--target-home /absolute/new-home --target-db-env MIGRATION_TARGET_DB
  --target-key-env MIGRATION_TARGET_KEY --target-stopped)

qwenpaw-data migration prepare "${migration_target[@]}" \
  --source-home /absolute/old-home --backup /absolute/backup --source-stopped \
  --identity-map identities.json --source-key-env MIGRATION_SOURCE_KEY \
  --context-trigger-ratio 0.8 --context-reserve-ratio 0.1 \
  --recovery-plan /absolute/private/plan.enc --run-id "$MIGRATION_RUN_ID" \
  --deployment-config-confirmed

qwenpaw-data migration apply "${migration_target[@]}" \
  --recovery-plan /absolute/private/plan.enc --confirm-run-id "$MIGRATION_RUN_ID" \
  --deployment-config-confirmed

# After an interrupted/ambiguous import, keep writers stopped and use the SAME plan:
qwenpaw-data migration recover "${migration_target[@]}" \
  --recovery-plan /absolute/private/plan.enc --confirm-run-id "$MIGRATION_RUN_ID" \
  --deployment-config-confirmed
```

Add the same model-size and explicit cron-disabling flags used during `plan`.
`prepare` revalidates conversion, Auth, conflicts and all existing credentials;
it writes only a persistent database identity token, lock file and private
authenticated encrypted plan, not business rows or workspace files. Existing
plans are refused rather than regenerated: randomized ciphertext and timestamps
must remain exact. If preparation fails, inspect its output before retrying.
The saved plan is bound to the home inode, database selection and recorded token.
Database clones/restores require operator review, even at the same URL.

`apply` and `recover` share the same idempotent coordinator. Source may be offline;
an uncommitted import requires the independent backup online and verified. SQL
rows and their receipt commit together around file publication; SQLite recovery
uses its engine, never manual journal deletion. Files are mode-0600 non-overwriting
hard links from verified staging, requiring the same filesystem. Already committed
imports only verify, never repair changed rows, moved files or modified bytes.
Both commands require the exact run ID; connection errors never justify cleanup.

### Check and roll back an unchanged import

```bash
qwenpaw-data migration check-rollback "${migration_target[@]}" \
  --recovery-plan /absolute/private/plan.enc
qwenpaw-data migration rollback "${migration_target[@]}" \
  --recovery-plan /absolute/private/plan.enc --confirm-run-id "$MIGRATION_RUN_ID"
```

`check-rollback` verifies backup, unchanged SQL and file evidence, then scans known
native and conservative JSON references. Exit `2` reports dependencies. Unknown
tables/columns, triggers, added foreign keys and scan-budget excesses refuse the
operation. This does not prove external dependencies or arbitrary references in
prose/code; review those before confirming rollback. It may create the home lock
file but does not change business rows or workspace files.

`rollback` repeats dependency checks inside its deletion transaction. It records
encrypted local intent before SQL commit, then requires a durable SQL rollback
receipt before unlinking only unchanged, originally owned published files. An
ambiguous response retains files; retry **rollback**, not `recover`, with the same
plan and run ID. Crashes between unlinks are resumable; a replaced, moved, modified
or additionally linked file is never adopted or removed. Original and rollback
SQL receipts remain permanently; pending rollback intent and committed rollback
both forbid forward replay. Rollback uses the original selected key even if the
runtime key configuration was subsequently changed.

### Retention and acceptance boundary

Keep source, independent backup, original keys, encrypted plan, private staging,
publication/rollback journals and SQL receipts. No automatic staging or directory
cleanup is provided: directories are only observed, never claimed as removable.
The retained staging copy shares an inode with a published file, so it is **not an
independent backup**; application changes cause later verification/rollback to
refuse. Do not unlink the migration lock. Interrupted initial journal creation,
unexpected links or eight incomplete staging attempts require operator inspection.

Fresh-process SQLite/PostgreSQL acceptance covers 15 tables and two files through
the actual CLI workflow, plus coordinator crash drills at copy, publication, SQL
commit, rollback intent, every unlink and completion. Native Host reads across
restart were verified separately. This does not prove external backends, remote
deployment configuration or native Windows support. The home lock excludes only
cooperating migration invocations; applications must remain stopped throughout.

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

Migration commands do not configure persistent CLI logging and temporarily
suspend existing CLI-owned file handlers, including on failure, so selecting
the old home as `QWENPAW_DATA_HOME` does not append or rotate its logs. They still
print JSON reports and concise errors; external handlers installed by an
in-process caller remain that caller's responsibility.

This package intentionally omits `serve` and local initialization helpers from
the public CLI help.
