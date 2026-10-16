# PawApp vNext: durable Engine submissions

This contract lets a trusted PawApp backend start an independent analysis
without a Main Chat turn or an existing Engine session. The Engine owns
execution and its replay log; the QwenPaw Host owns task identity, scope checks,
Direct/Delegated presentation, and any Main Agent continuation.

## Release boundary after runtime 0.3.2

This branch is based on `14535e3` (QwenPaw-Data #81). Runtime 0.3.2 already
provides the native console API, per-turn model bindings, datasource/dataset
selection, scoped session ownership, readable session names, and the Windows
artifact-path fixes. Those features and the release dependency updates are
inherited from `main`, rather than reintroduced by this branch.

The remaining changes add durable submissions and commands, task-scoped Host
capabilities, verified artifact handoff, and analysis progress/presentation.
The native console continues to use its existing API; a Host adapter opts into
the durable protocol through capability discovery. Package versions remain at
the 0.3.2 base until a separate release is prepared.

## Capability discovery

This branch implements **submission protocol 1**. It does not declare a released
Engine version compatible with all of PawApp vNext. Adapters must probe
`GET /api/v1/capabilities/submissions` and require `protocol_version: 1`,
`durable_submissions: true`, and `event_replay: true`. Task answer/cancel also
requires `durable_commands: true`. Older Engines lack this
endpoint; JSON mode reports unsupported and returns 501 for submit/query/replay.
Do not silently fall back to non-idempotent session/chat creation.

Engines that accept task-scoped Host tools and Skills additionally advertise
`scoped_host_capabilities: true`. A Host that supplies a capability envelope
must require this flag before submission; older Engines remain compatible with
submissions that do not carry an envelope.

Engines used by an artifact-aware adapter advertise `artifact_handoff: true`.
After every successful tool result they emit `artifact.registered` for changed
files even when BizTrace is disabled. Each event includes normalized path,
media type, byte size, and `sha256:` digest. A Host copies the file through
`GET /api/v1/sessions/{session_id}/artifacts/file?path=...&digest=...`; the
Engine requires the caller to own the session and returns 409 if the live file
no longer matches that immutable event version.

`GET /api/v1/capabilities/analysis` returns readiness version 1 and the boolean
`model_configured`. It checks the explicit model, or the same local Agent
Configuration preferences/environment fallback used by independent runs.
With `local_only=true`, it instead checks the trusted caller's saved default
model. No credentials
or configuration error details are returned, and no provider call is made.
This is configuration readiness, not a connectivity guarantee. The Host also
checks its authorized datasource against `/api/v1/datasources`, then returns a
blocked result with the App settings entry before creating a task if setup is
missing. Durable submission protocol support alone does not imply readiness.

## Submit and reconcile

### Additive capabilities: analysis experience and existing sessions

`analysis_experience: true` advertises typed `analysis.progress` events with
stage `read_data`, `confirm_scope`, `analyze`, or `publish_report`. Private
runtime tools let the domain agent declare stages and publish selected files.
Ordinary artifact discovery emits explicit `diagnostic` / `app_only` metadata;
filenames are not a publication decision. Intentional artifact publication
verifies the file and adds the shared presentation fields: `schema_version: 1`,
`role`, `kind`, `visibility`, `preview`, and `rank`. Host owns generic rendering
and authorization; Engine owns the domain meaning and selected deliverables.

`session_submissions: true` permits optional `session_id`, `attachment_ids`,
and `artifact_comments` in the submission below. The existing session must
belong to the supplied trusted namespace. Attachments must belong to the same
session and namespace. A running turn blocks a second new submission; retries
of the original receipt remain idempotent. Session update, chat creation and
receipt commit in one transaction. Subsequent completed turns may reuse the
same session. All context participates in the digest; omitted/empty additive
fields retain the original protocol-1 digest for old accepted submissions.
`GET /api/v1/sessions/{session_id}/submission-readiness` checks ownership and
returns `ready: false` while a turn is running; unavailable/foreign sessions
return 404. This read-only preflight prevents admission of legacy or busy
sessions; the transactional submission still rechecks both conditions.

The vNext Host adapter creates native sessions under the same scoped
namespace, admits each turn as a Direct task, and routes answers/cancellation
through durable commands. It must not forward browser raw submissions. Old
unscoped sessions require an explicit migration or a new session, not implicit
ownership reassignment. Dataset selection is supported when `dataset_context`
is advertised: `dataset_names` is validated, persisted on the chat, and included
in the request digest and agent input. New sessions require a datasource;
existing sessions may omit it when `optional_session_datasource` is advertised.
Disabling clarification is not supported by this submission contract and must
not be silently ignored.

```http
POST /api/v1/submissions
Authorization: Bearer <backend service token>
X-User-Id: <stable caller namespace>
Content-Type: application/json

{
  "protocol_version": 1,
  "submission_id": "sub_host_generated_id",
  "text": "Analyze revenue for the selected datasource",
  "datasource_id": "ds_revenue",
  "agent_id": "default",
  "capability_bridge": {
    "protocol_version": 1,
    "endpoint": "http://127.0.0.1:8088/api/pawapp-capabilities",
    "token": "<task-bound bearer>"
  }
}
```

`capability_bridge` is optional and ephemeral. The Engine keeps it in the run's
in-memory request context, uses it only for the Host capability protocol, and
never writes it to workspace files or event streams. The endpoint must be an
absolute HTTP(S) URL without credentials, query, or fragment. The token is
included in the idempotency digest, so a Host retry must reproduce the same
envelope for a given durable submission.

`protocol_version` and `agent_id` default to 1 and `default`. The ID accepts
1–128 ASCII letters, digits, underscores, or hyphens. `text` and `datasource_id`
must be nonblank; unknown fields and unsupported protocol versions are rejected.
Datasource existence, model readiness, and analysis execution can still fail
after acceptance. Acceptance is not a readiness guarantee.

Successful acceptance and identical retries both return **202**:

```json
{
  "protocol_version": 1,
  "submission_id": "sub_host_generated_id",
  "state": "accepted",
  "run": {
    "session_id": "ses_generated_id",
    "run_id": "chat_generated_id",
    "status": "running",
    "last_sequence_number": -1,
    "error": null,
    "reason": null
  }
}
```

The `submissions` SQL table has a unique `(user_id, submission_id)` key and
stores a request digest plus immutable session/run IDs. The digest covers the
effective protocol version, agent, text, datasource, and optional capability
envelope. The receipt, new
session, and first chat commit in **one transaction**. Only the transaction
winner schedules the runtime; concurrent identical retries return its mapping.
Reusing an ID with different inputs returns **409**. Different submission IDs
create different sessions, allowing concurrent delegations to the same App.

HTTP request cancellation does not cancel the acceptance/scheduling operation.
A process exit after commit but before scheduling leaves an accepted run that
startup recovery marks interrupted; it never schedules a replacement run.
This is deduplicated acceptance, not an exactly-once guarantee for external
tool side effects.

`GET /api/v1/submissions/{submission_id}` returns:

| Outcome | Meaning for the Host adapter |
|---|---|
| 200, `state: accepted` | Bind the original session/run IDs; attach using the last cursor committed by Host. |
| 200, `state: not_found`, `run: null` | No receipt exists in this caller namespace. Retry with the **same** ID and inputs only if Host has never bound a run. |
| Timeout, 5xx, invalid response, unavailable Engine | Submission outcome is **unknown**. Keep the original ID and reconcile later; never reinterpret transport failure as `not_found`. |
| Accepted with `run.status: unknown`, `reason: run_missing` | Receipt remains, but its run is unavailable. Preserve the mapping and report unresolved; do not create a replacement. |

Receipts have no TTL and are not deleted when a session is soft-deleted. If a
run is missing, replay returns 410 and retries still return the original receipt.
Durability assumes the same persistent database: deleting/restoring/replacing
that database is not a supported transparent failover operation.

## Durable answer and cancel commands

`POST /api/v1/submissions/{submission_id}/commands` applies a command to the
original accepted run. The request contains `protocol_version: 1`, a stable
`command_id`, and one of these payloads:

```json
{
  "command_id": "answer_clarification_1",
  "kind": "answer",
  "request_id": "clarification_1",
  "answers": [{
    "question": "Which period?",
    "selected_options": ["Q1"],
    "custom_text": null
  }]
}
```

```json
{
  "command_id": "cancel_task_1",
  "kind": "cancel",
  "reason": "No longer needed"
}
```

The `submission_commands` table permanently binds
`(user_id, submission_id, command_id)` to a kind and request digest before the
runtime is touched. Identical retries return the stored receipt and never apply
the effect twice. Reusing a command ID with different content returns 409.
Commands and their lookups use the submission's caller namespace, so a receipt
cannot be read or applied through another namespace.

The response state is `accepted`, `rejected`, or `unknown`. An answer is rejected
with `reason: stale_request` when its clarification is absent, already resolved,
or no longer belongs to the active runtime. Cancel targets the bound run; a
terminal run returns an accepted receipt with `reason: already_terminal` and
retains its output. A crash after the command receipt is prepared but before an
outcome is committed is reported as `unknown`; it is never guessed from a lost
HTTP response.

`GET /api/v1/submissions/{submission_id}/commands/{command_id}` returns the same
receipt, `unknown` for a prepared command, or `not_found` when no receipt exists.
The Host must persist its own command before POST, reconcile an uncertain send by
this lookup, and keep the same command ID on every retry.

## Replay and recovery

`GET /api/v1/submissions/{submission_id}/events` resolves the receipt under the
same caller namespace and reuses the existing chat SSE stream. `run_id` is the
Engine's `chat_id`; it is not a second execution identity. Every persisted frame
contains session/chat IDs and a dense, monotonic `sequence_number` (starting at
0); its SSE `id` is the cursor. Pass `Last-Event-ID` or `after_sequence_number`
on reconnect; when both are present, the larger cursor wins.

Host must persist its task→session/run mapping and consumed cursor with its own
task/event update. `last_sequence_number` in lookup is an Engine watermark,
**not** permission to skip events Host has not consumed. Reconnect can redeliver
events; consumers deduplicate by run identity and sequence.

| Failure | Result |
|---|---|
| Host disconnect/reconnect, Engine still running | Replay from Host's committed cursor, then follow live events; no new analysis. |
| Engine restart with a persisted terminal response | Preserve its outcome and replay the same terminal event. |
| Engine restart with an unfinished accepted run | Atomically persist cancellation plus an error with `details.reason: executor_restarted`; submission status is `interrupted`. No automatic rerun. |
| Chat row says `completed`, but no terminal response exists | While running, lookup says `reconciling`; after restart, `interrupted`. The runtime may have lost final message data before flushing it. Never synthesize success. |
| Persisted failed/canceled chat lacks its final response | Startup appends the corresponding terminal event without executing again. |

For wire compatibility, interrupted runs use a `response` event with
`status: cancelled`, `error.code: VALIDATION`, and
`error.details.reason: executor_restarted`. The adapter maps that reason to
Host `interrupted`, separately from user cancellation. Lookup statuses are
`running`, `reconciling`, `succeeded`, `failed`, `cancelled`, `interrupted`, or
`unknown`. A successful outcome requires a persisted `response/completed`;
**EOF is never success**. Result text remains in the existing message/content
events and must be reconstructed by the adapter, including full snapshots
versus deltas.

Startup recovery runs before requests or scheduled jobs and commits chat state,
any missing terminal event, and its sequence watermark together. Recovery can
run repeatedly without appending duplicate terminal events. Existing console
and session/chat APIs retain their behavior; their POST endpoints do not gain
idempotency from this protocol.

## Trust and deployment boundary

Run one Engine process/worker per database. SQL unique keys protect concurrent
submissions, but the execution registry and live event fan-out remain in one
process; this is not a multi-worker scheduler. SQLite/WAL is the verified
backend. The SQL uses SQLAlchemy's portable expressions, but PostgreSQL recovery
has not been exercised in this change.

All routes use the existing service bearer/loopback middleware. `X-User-Id`
partitions receipts as a **trusted backend identity stamp**, not an authenticated
end-user principal. A service token holder can choose that header. The Host
must derive a stable namespace from its trusted context, enforce user/workspace/
App permissions, and keep the Engine token out of the browser. This change
does not add tenant isolation to older session/chat endpoints.

## Validation and integration boundary

The API tests use real SQLite transactions and HTTP routes with a controlled
runtime; no model/provider calls are needed. They cover concurrent duplicate
submissions, input conflicts, separate runs and caller namespaces, transaction
rollback, lost responses, cancellation of HTTP requests, hard process exit
after commit, restart recovery, live disconnect/replay, retained receipts after
history deletion, unavailable databases, bearer checks, and unsupported JSON
mode. Command tests cover idempotent answer/cancel effects, content conflicts,
stale clarification receipts, namespace isolation, and the prepared/unknown
recovery window. Existing host-core regression tests cover console/chat
compatibility.

The QwenPaw Host Data adapter consumes these submission and command routes and
projects replayed Engine output into Host TaskEvents. Main Agent continuation
with arbitrary follow-on tools, grant UI, and public/private skill/tool access
remain separate implementation gates. The protocol probe above must not be used
to claim those gates are complete.
