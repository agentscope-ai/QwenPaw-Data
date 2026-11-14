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

## Legacy JSON migration preparation

`migration inspect`, `backup`, `plan` and explicit `provision-key` are available;
**there is no import/apply
command yet**. Stop all source writers before backup/planning. Use a separate
backup and target home; source data is never overwritten. For example:

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
false`: transactional file/SQL import, idempotence and rollback are not yet
implemented. Preflight is a point-in-time check, not a
reservation; import must revalidate all mutable state.
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

The Host now includes an internal transactional SQL component and the additive
`legacy_import_runs` receipt table. Rows and the SQL receipt commit together;
exact-plan replay verifies stored row digests and never overwrites changed data.
Connection failures are not treated as proof of rollback. Explicit SQLite
engine recovery is separate from read-only preflight and never manually deletes
journals. This component is **not an import command**: the file components below
must be integrated into one supported recovery/rollback workflow before a CLI
`apply` can be exposed. Do not invoke the SQL component alone to migrate user data.

An internal POSIX recovery-plan component now preserves the exact converted rows,
ciphertext, timestamps and scoped file inventory in an authenticated encrypted
0600 file. Publication requires a verified independent backup and stopped source
and target writers; it never replaces an existing different plan. It binds the
plan to the target home directory, an opaque database selection fingerprint and
an explicitly recorded `legacy_import_targets` token. Recovery must check that
token inside every SQL receipt/import transaction: a missing or replaced database
is not evidence that an import rolled back. Password rotation is allowed; database
clones/restores still require operator review because they can copy the token.

Loading this plan does not reconnect or reread the source/backup. Fresh-process
SQLite and PostgreSQL checks cover SQL precommit crashes, lost postcommit
responses and exact replay from the saved plan. This does **not** permit deleting
the backup, prove attachment publication, or provide a CLI recovery command.
Unexpected links (including a crash between no-replace publication and temporary
link removal) are refused for operator inspection, not automatically deleted.
The internal file-preparation component now uses a process-held POSIX lock per
canonical target home (never unlinking the lock file). With application writers
stopped, it copies only from the verified independent backup into a separate
private staging area; original modes are not restored and copies stay 0600.
Authenticated receipts bind each completed copy to its exact plan and file inode.
Repeated preparation rechecks hashes and refuses modified files, aliases, changed
targets or an already committed SQL import. Fresh-process SQLite/PostgreSQL
acceptance covers a crash during copying and reuse of completed staging.

Interrupted copies are retained without overwrite/deletion; at most eight
incomplete attempts are allowed by default before operator inspection is required.
An interruption before the initial staging-root binding is durable also requires
inspection, not automatic adoption. This lock only excludes cooperating migration
processes on one home; it does not stop Host writers or coordinate different
machines/homes sharing a database.

An internal publication hook now places verified staged files into their scoped
workspace destinations using atomic, non-overwriting hard links. An authenticated
intent is durable before each link; retries require the same staged inode and
matching intent, not merely equal target bytes. Staging and destination must be on
the same filesystem. Files stay 0600; existing safe directories are observed but
never claimed as removable. Keep the staging links and independent backup: normal
application writes also change the linked staging copy, and later verification
then refuses replay instead of overwriting user changes.

SQLite and PostgreSQL process checks cover first-file publication crashes,
all-files-published/precommit crashes, lost SQL commit responses, and exact replay
of all 15 tables plus two final files. The hook belongs inside the anchored SQL
transaction; replay of an already committed SQL receipt must separately verify
file publication.

The internal saved-plan coordinator now performs that sequence under one home
lock: authenticate the original plan, recover/check the bound SQL receipt, stage
or resume files, commit SQL with the publication hook, and independently verify
published files. Uncommitted imports require the independent backup online and
verified; the source may be offline. Already committed imports only verify SQL
and files and may be checked without backup access. They never repair modified
rows, changed files or files moved by the user. Errors preserve all evidence and
must be retried with the same saved plan, not a newly generated conversion.

Fresh-process SQLite/PostgreSQL acceptance covers interrupted copying, partial
publication, all-files/precommit exit and lost commit responses through this
entry point. A real local Host twice across restart verified artifact preview,
download and compatibility access, native attachment resolution, authorization
rejection, imported history/preferences and the separately provisioned local key.
Receipts still verified after use. This does not configure remote deployments or
prove external backends. A supported CLI `apply`, post-import rollback, safe
journal/staging cleanup and native Windows acceptance remain unfinished.

The internal rollback-eligibility check now verifies the saved plan, independent
backup, unchanged imported SQL rows and publication evidence before examining
remaining native references. It detects session/chat/attachment/model references
including conservative nested-JSON candidates, without deleting rows or files.
Unknown tables, extra columns, native-table triggers and added foreign keys into
imported tables require review; bounded scans fail closed when their row/JSON
budgets are exceeded. Unrelated existing data does not automatically block the
check. This is **not rollback authorization**: external dependencies and arbitrary
references embedded in prose/code are not proven, and the checks must run again
inside the deletion transaction.

An internal SQL rollback component now repeats these checks transactionally,
deletes only the unchanged imported primary keys in bounded batches, and commits
a permanent `legacy_import_rollbacks` receipt atomically with those deletions.
The original import receipt remains; the same run cannot be replayed forward.
Rollback replay checks that the deleted keys remain absent and that new references
have not appeared. Missing schema or an unreadable receipt is not evidence of
success. Initialize the additive receipt table through normal target schema
alignment separately; rollback never creates or repairs schema.

Independent SQLite/PostgreSQL process checks cover precommit exit, lost rollback
responses and exact receipt replay, with Auth data and all file bytes/inodes
retained. This is **not full rollback**: its required coordinator hook must verify
file ownership and persist rollback intent without deleting files before SQL
commit is confirmed. Production rollback-intent journaling, file rollback,
safe cleanup and supported CLI apply/rollback remain unfinished. Keep the original
source, independent backup, encrypted plan, staging links and both SQL receipts.

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
