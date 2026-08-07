# Runtime Resource Governance

DataPaw applies request-level limits across uploads, SQL, Cypher, graph
traversal, LLM work, response materialization, and request lifetime. Defaults
are safe for a local, single-user deployment and can be lowered for constrained
machines.

| Resource | Environment variable | Default |
| --- | --- | --- |
| Whole-request deadline | `DATAPAW_REQUEST_TIMEOUT_SECONDS` | 120 seconds |
| Upload/request body | `DATAPAW_MAX_UPLOAD_MB` | 50 MiB |
| Materialized response target | `DATAPAW_MAX_RESPONSE_MB` | 50 MiB |
| SQL result rows | `DATAPAW_MAX_SQL_ROWS` | 10,000 |
| Cypher result rows | `DATAPAW_MAX_CYPHER_ROWS` | 1,000 |
| Graph nodes | `DATAPAW_MAX_GRAPH_NODES` | 5,000 |
| Graph edges | `DATAPAW_MAX_GRAPH_EDGES` | 20,000 |
| LLM tokens per request | `DATAPAW_MAX_LLM_TOKENS` | 32,768 |
| LLM retries per request | `DATAPAW_MAX_LLM_RETRIES` | 4 |

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

## Blocking I/O and event-loop safety

DataPaw uses two explicit boundaries for synchronous libraries:

1. A FastAPI `def` endpoint is appropriate when the complete handler is
   synchronous. FastAPI runs it through AnyIO's worker pool, whose process-wide
   capacity is configured by `DATAPAW_SYNC_ROUTE_WORKERS`. The standard
   launcher bounds the outer request queue with
   `DATAPAW_HTTP_MAX_CONCURRENCY`; other ASGI deployments must configure an
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
DATAPAW_BLOCKING_<POOL>_WORKERS
DATAPAW_BLOCKING_<POOL>_MAX_QUEUE
DATAPAW_BLOCKING_<POOL>_QUEUE_TIMEOUT_SECONDS
DATAPAW_BLOCKING_<POOL>_TIMEOUT_SECONDS
```

`DATAPAW_BLOCKING_SHUTDOWN_TIMEOUT_SECONDS` controls graceful drain during ASGI
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
`${DATAPAW_HOME:-~/.datapaw}/data-bridge/state/jobs.db`. The SQLite store uses
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
