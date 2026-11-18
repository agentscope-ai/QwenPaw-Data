# qwenpaw-data-tracing

Shared OpenTelemetry plumbing for exporting QwenPawData traces to Langfuse v4
(or another OTLP backend). Three blocks; `export` and `runtime` import `core`. The
package does not depend on host or context.

```
qwenpaw_data.tracing
  core/      span, ScoreEvent, names
  export/    install, OTLP, score HTTP
  runtime/   host / cm adapters; common is shared
```

Install from env:

```python
from qwenpaw_data.tracing import Settings, tracer_install

tracer_install(Settings.from_env().to_config(service_name="qwenpaw-data-context"))
```

Synchronous scripts can call `tracer_send_span_score` inside a valid span,
then `tracer_shutdown()` before exiting. Submission does not wait on HTTP;
up to 1,000 standalone scores are retained for best-effort delivery to their
original exporters within the shutdown budget. In async services, await
`flush_span_scores()` on the producing loop **before** stopping that loop.

Call-site imports:

```python
from qwenpaw_data.tracing.core.names import Step, Score, Tracer
from qwenpaw_data.tracing.runtime.host.chat import ChatTracer
from qwenpaw_data.tracing.runtime.host.chat_id import ChatTraceIdGenerator
```
