# Runtime Resource Governance

QwenPaw Data applies request-level limits across uploads, SQL, Cypher, graph
traversal, LLM work, response materialization, and request lifetime. Defaults
are safe for a local, single-user deployment and can be lowered for constrained
machines.

| Resource | Environment variable | Default |
| --- | --- | --- |
| Whole-request deadline | `QWENPAW_DATA_REQUEST_TIMEOUT_SECONDS` | 120 seconds |
| Upload/request body | `QWENPAW_DATA_MAX_UPLOAD_MB` | 50 MiB |
| Materialized response target | `QWENPAW_DATA_MAX_RESPONSE_MB` | 50 MiB |
| SQL result rows | `QWENPAW_DATA_MAX_SQL_ROWS` | 10,000 |
| Cypher result rows | `QWENPAW_DATA_MAX_CYPHER_ROWS` | 1,000 |
| Graph nodes | `QWENPAW_DATA_MAX_GRAPH_NODES` | 5,000 |
| Graph edges | `QWENPAW_DATA_MAX_GRAPH_EDGES` | 20,000 |
| LLM tokens per request | `QWENPAW_DATA_MAX_LLM_TOKENS` | 32,768 |
| LLM retries per request | `QWENPAW_DATA_MAX_LLM_RETRIES` | 4 |

The ASGI middleware enforces the deadline without consuming and rebuilding a
business response. Upload limits validate both `Content-Length` and actual
bytes received. SQL, Cypher, and explorer handlers clamp caller-provided
limits before executing or materializing results. The blocking-I/O governor
independently bounds active workers and admission queues for each resource
class.

Budget failures use a typed JSON error with a stable code, retryability flag,
and limit details. A timeout returns HTTP 504; capacity/result limits return
HTTP 429 or a route-specific 413 where appropriate.

Streaming responses remain subject to the whole-request deadline. Increase
the deadline explicitly for a trusted long-running deployment rather than
removing the limit. The middleware never truncates a response after headers
are sent and never rewrites `Content-Length`.

## Host tool deadlines and SQL results

`QWENPAW_DATA_TOOL_TIMEOUT_SECONDS` bounds a complete async tool call, including
SQL CSV materialization. The default is 1,800 seconds; interactive deployments
can set a shorter limit, for example `120`. The middleware is installed on main
and child agents. On timeout it cancels the tool task, discards partial results,
and returns `TOOL_TIMEOUT`. External cancellation propagates normally. Cancellation
is cooperative: connector-side statement/process limits remain necessary for
remote work or blocking libraries. This is not a whole-turn or model deadline.

Spawned agents use the same tool deadline, including SQL artifact handling, and
propagate cancellation. Cancelling or closing a spawn stream drains its pending
child event task and closes the child stream before releasing the concurrency
slot. The same cleanup runs before returning a child timeout or execution error.

Materialized SQL responses retain `file_path` and additionally return `result_id`.
Downstream Python tools can resolve it without asking the model to copy rows,
counts, or status fields:

```python
from qwenpaw_data.host.core.sql_results import load_sql_result

result = load_sql_result(result_id, artifact_dir=current_session_artifact_root)
```

Records live in the session's `data/results/` directory. Resolution checks the
session-local record and CSV SHA-256; a missing or changed CSV is rejected. The
result metadata retains `truncated`, so consumers must still check completeness
and validate their own analytical requirements. The caller must supply the
trusted current session root, not an arbitrary model-selected directory.

## Dataset-scoped SQL

Context packs separately expose dimension `bindings` scoped to the metric's
formula datasets. Each includes a physical table/column pair when uniquely known,
the dataset column, expression and filter. Multiple bindings remain separate;
singular compatibility fields are populated only for one binding. Missing or
composite lineage is not represented as a confirmed physical column. These hints
help SQL generation and do not replace runtime column validation.

Host applications may set `request_context={"datasource_id": "...",
"dataset_names": ["confirmed_dataset"]}`. Host overrides model-supplied scope;
the DataBridge MCP forwards it to both the normal SQL API contract and its
streaming equivalent. REST callers may supply `dataset_names` directly.

Before cache lookup or execution, DataBridge resolves each dataset on that
datasource. Confirmed single-parent logical references (bare, `cm_view` or the
registered physical schema) are rewritten using SQL syntax trees; multi-parent
datasets must be queried by confirmed physical names. Declared dataset filters or
SQL transformations cannot be dropped by this rewrite. The rewritten SQL is checked
against the selected datasets' `parents` mappings (or their own names when no
mapping exists), then used for both cache lookup and execution. Scoped execution
does not retry through the legacy view-not-found fallback, and cached results
whose executed SQL differs from the confirmed SQL are ignored. CTEs and subqueries use SQL scopes;
an explicitly qualified table must match the registered schema. Missing or
ambiguous datasets, graph failures, unsupported statements/table functions and
unparseable SQL are rejected. Omitting `dataset_names` preserves unscoped behavior;
an explicitly empty list is rejected. Cache entries are isolated by datasource.

Unscoped view-not-found retries also use SQL syntax trees and resolve dataset
parents only on the active datasource. They preserve aliases, literals and column
identifiers; unresolved or ambiguous mappings retain the original query error.

This is a table correctness guard, not an authorization boundary. It does not
check columns, metric meanings, dates or regional scope. Existing connector
read-only and resource controls continue to apply. Upgrade both Host and
DataBridge when enabling it; old MCP servers do not support `dataset_names`.

## PostgreSQL and MySQL statement limits

`QP_SQL_STATEMENT_TIMEOUT_MS` defaults to 45,000 ms (`0` disables). PostgreSQL
uses a per-connection `statement_timeout`; MySQL receives an outer SELECT
`MAX_EXECUTION_TIME` hint, including WITH and UNION queries, plus a client read
timeout. Existing shorter connector read limits are preserved. These settings
do not add statement limits to DuckDB, SQLite or ODPS.

A database-reported statement timeout returns `QUERY_TIMEOUT`. A MySQL client
read timeout returns `SQL_READ_TIMEOUT` and does not imply remote cancellation.
Neither proves excessive data volume. Query optimization must preserve the
requested analytical scope; a client must not silently shorten the date range.

`QP_SQL_RESULT_CACHE_TTL_SECONDS` sets the result-cache TTL (default 86,400).
A live cache entry can renew once, so repeated hits cannot extend its lifetime
beyond twice the TTL. Expired entries cannot be revived by a touch.

## Blocking I/O and event-loop safety

QwenPaw Data uses two explicit boundaries for synchronous libraries:

1. A FastAPI `def` endpoint is appropriate when the complete handler is
   synchronous. FastAPI runs it through AnyIO's worker pool, whose process-wide
   capacity is configured by `QWENPAW_DATA_SYNC_ROUTE_WORKERS`. The standard
   launcher bounds the outer request queue with
   `QWENPAW_DATA_HTTP_MAX_CONCURRENCY`; other ASGI deployments must configure an
   equivalent ingress/concurrency limit.
2. An `async def` endpoint must call synchronous code through
   `request.app.state.blocking_io`. The governor provides isolated `graph`,
   `file`, `network`, and `sql` pools with bounded active work and finite
   admission queues. It also propagates request `contextvars`, including the
   selected Neo4j logical database, into worker threads.

Direct `asyncio.to_thread`, the loop default executor, synchronous Neo4j or
OpenAI calls, openpyxl parsing, and filesystem reads/writes are not allowed in
an async HTTP request path. Existing dedicated executors may remain only when
they provide equivalent concurrency, queue, timeout, shutdown, and metrics
semantics.

### Overload and timeout semantics

- A full admission queue returns HTTP 503 with `Retry-After: 1`; admitted work
  that cannot start before its queue deadline returns the same response.
- Operation wait expiry returns HTTP 504.
- Cancelling or timing out an await does not stop a Python thread. Its worker
  slot remains occupied until the callable exits.
- Every governed operation therefore also needs a hard dependency timeout,
  such as a Neo4j query/transaction timeout, an HTTP client timeout, or a SQL
  statement timeout.
- Excel parsing and other CPU-heavy work stays in the file pool for bounded
  use. Work that regularly exceeds an HTTP deadline should move to a durable
  background job rather than increasing the pool timeout.

### Configuration

Each pool supports the following variables, where `<POOL>` is `GRAPH`, `FILE`,
`NETWORK`, or `SQL`:

```text
QWENPAW_DATA_BLOCKING_<POOL>_WORKERS
QWENPAW_DATA_BLOCKING_<POOL>_MAX_QUEUE
QWENPAW_DATA_BLOCKING_<POOL>_QUEUE_TIMEOUT_SECONDS
QWENPAW_DATA_BLOCKING_<POOL>_TIMEOUT_SECONDS
```

`QWENPAW_DATA_BLOCKING_SHUTDOWN_TIMEOUT_SECONDS` controls graceful drain during ASGI
shutdown. The `/api/health` response exposes active, queued, rejected, timed
out, failed, and cumulative wait/run values for each pool, along with
event-loop lag and access-log queue metrics.

### Review checklist

- Is the endpoint correctly declared as `def` or `async def`?
- Does every synchronous call from `async def` go through the governor?
- Is the resource pool classification correct?
- Does the underlying client enforce a hard timeout?
- Is result consumption bounded or streamed?
- Is overload returned to the caller instead of queued without limit?
- Does cancellation leave shared state consistent?
- Are shutdown and regression tests included?

## Durable jobs and plans

Import task status and semantic preview/confirm plans are stored in
`${QWENPAW_DATA_HOME:-~/.qwenpaw-data}/data-bridge/state/jobs.db`. The SQLite store uses
WAL and full synchronous commits, and supports:

- queued/running/succeeded/failed/cancelled state;
- idempotency keys (`Idempotency-Key` on physical imports);
- worker lease and heartbeat renewal;
- bounded attempts and stale-lease recovery;
- TTL expiry for short-lived plans;
- atomic single-winner plan consumption;
- restart recovery that marks interrupted unleased work failed.

Embedding rebuild jobs retain their specialized resume implementation. Other
interrupted jobs expose an explicit failed state and require retry/re-preview;
they are not silently reported as successful or automatically replayed.
