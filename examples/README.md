# GAAP demo environment

This directory contains the complete synthetic demo for the question:

> Analyze the average GAAP value of valid users for product X during March
> 2026, show the trend, and explain the largest spike.

The dataset is fictional and reproducible. It contains 475 rows in
`dws_gaap_di`, uses a fixed random seed, and has a deliberate spike on
2026-03-10. A valid user is one whose fiscal year-to-date GAAP value is at
least 10 USD.

## Included assets

| Asset | Purpose |
| --- | --- |
| `demo_semantic_config.xlsx` | Semantic domain, dataset, columns, dimensions, metric and formula; import binds to an explicitly registered datasource |
| `demo_kg_doc.docx` | Knowledge Graph document containing the valid-user definition and business events associated with the spike |
| `init_demo.sh` / `init_demo.ps1` / `init_demo.py` | Repeatable SQLite and PostgreSQL initialization, verification, and DataBridge registration |
| `smoke_test.py` | Deterministic real-CLI/DataBridge/SQL test using a local model stub |
| `semantic_smoke_test.py` | Deterministic datasource + semantic CLI test (CRUD, import, weave); no model key |

## Fast deterministic test (no model API key)

Complete the root initialization first. Start the local services in terminal A;
the frontend is not needed for this test:

```bash
# macOS / Linux
scripts/start_local.sh --skip-frontend
```

```powershell
# Windows PowerShell
.\scripts\start_local.ps1 --skip-frontend
```

In terminal B, start and seed the bundled PostgreSQL database, then run the
smoke test using the commands for your platform:

```bash
# macOS / Linux
examples/init_demo.sh
uv run python examples/smoke_test.py
```

```powershell
# Windows PowerShell
.\examples\init_demo.ps1
uv run python .\examples\smoke_test.py
```

The smoke test starts isolated Host and DataBridge APIs with temporary shared
SQLite state. Host initializes its Auth/SQL tables before DataBridge starts.
An OpenAI-compatible local stub requests native MCP `execute_read` against real
PostgreSQL. The CLI response must contain the expected aggregates, including
the 2026-03-10 average of `45.89`, and Host must persist a completed SQL chat.
The fixture explicitly uses a local workspace; Docker isolation is tested
separately. It does not reuse your runtime database or dotenv configuration.

`uv run python examples/semantic_smoke_test.py` additionally verifies datasource
management, explicit-datasource workbook import, partial edits, batch deletion,
graph weaving and reference-protected deletion. It uses a local deterministic
embedding fixture with real Neo4j, not a real semantic-quality model. Both
scripts stop their own processes and remove their temporary runtime state;
they seed the supplied PostgreSQL demo table, so use dedicated demo databases.

## Full interactive demo

### 1. Start QwenPaw Data

After configuring the root `.env` and running the platform initializer, start
the full local stack in terminal A:

```bash
# macOS / Linux
scripts/start_local.sh
```

```powershell
# Windows PowerShell
.\scripts\start_local.ps1
```

The UI is available at `http://localhost:3000` and the API at
`http://localhost:8765`.

### 2. Load the demo bundle

In terminal B, run:

```bash
# macOS / Linux
examples/init_demo.sh --register
```

```powershell
# Windows PowerShell
.\examples\init_demo.ps1 -Register
```

This one command:

1. creates and verifies `examples/demo/data/qwenpaw-data-demo.sqlite`;
2. starts PostgreSQL with Docker Compose and loads all 475 `dws_gaap_di` rows;
3. creates or reuses the PostgreSQL datasource `Demo PG - GAAP use case`; and
4. imports `demo_semantic_config.xlsx`, binding it to the server-generated
   datasource ID printed by the initializer.

The PostgreSQL service listens on `127.0.0.1:55432` by default:

```text
database: qwenpaw_data_demo
user:     qwenpaw_data
password: qwenpaw-data-demo
```

These are loopback-only demo credentials, not production credentials. Override
the host port with `QWENPAW_DATA_DEMO_POSTGRES_PORT`.

### 3. Build the semantic and knowledge graphs

Open the DataBridge UI:

1. In **Semantic Weaving**, select `Demo PG - GAAP use case` and submit a
   `FULL` weave.
2. In **KG Docs Management**, upload `examples/demo_kg_doc.docx`.
3. Wait until the document status is `ready`.

The Excel configuration is already imported by `--register`; do not import it
again manually. Configure Provider credentials in Host and select the Context
LLM and embedding models before graph publication or KG extraction. For a
SQL-only test, you can skip this graph-building step and
run the deterministic smoke test instead.

### 4. Run the analysis

Find the generated ID for `Demo PG - GAAP use case`:

```bash
qwenpaw-data datasource list
```

Then replace `DEMO_DATASOURCE_ID` and `HOST_ORIGIN` with that ID and your running
Host origin (for example, `http://127.0.0.1:8088`):

```bash
# macOS / Linux
qwenpaw-data run \
  --host-url HOST_ORIGIN \
  --no-stream \
  --datasource-id DEMO_DATASOURCE_ID \
  "Analyze the average GAAP value of valid users for product X during March 2026; show the trend over time and explain any spikes with relevant KG events"
```

```powershell
# Windows PowerShell
qwenpaw-data run --host-url HOST_ORIGIN --no-stream --datasource-id DEMO_DATASOURCE_ID "Analyze the average GAAP value of valid users for product X during March 2026; show the trend over time and explain any spikes with relevant KG events"
```

The expected peak is 2026-03-10, with an average GAAP value of approximately
`45.89` for valid product-X users. The Knowledge Graph document includes the
student renewal campaign and North enterprise renewal batch that explain the
spike.

## Inspect the data directly

SQLite is available without Docker:

```bash
# macOS / Linux
examples/init_demo.sh --sqlite-only
sqlite3 examples/demo/data/qwenpaw-data-demo.sqlite \
  "SELECT ds, ROUND(AVG(gaap_val), 2) FROM dws_gaap_di WHERE product = 'X' AND ytd_gaap >= 10 GROUP BY ds ORDER BY ds;"
```

```powershell
# Windows PowerShell (creates and verifies the same SQLite file)
.\examples\init_demo.ps1 -SqliteOnly
```

PostgreSQL can be queried after running either platform initializer:

```bash
docker compose -f examples/docker-compose.yml exec -T postgres \
  psql -U qwenpaw_data -d qwenpaw_data_demo -c \
  "SELECT ds, ROUND(AVG(gaap_val), 2) FROM dws_gaap_di WHERE product = 'X' AND ytd_gaap >= 10 GROUP BY ds ORDER BY ds;"
```

The same `docker compose ... psql` command can be entered as one line in
PowerShell.

## Stop and reset

Stop the demo PostgreSQL service without deleting its volume:

```bash
docker compose -f examples/docker-compose.yml down
```

Delete the demo database volume as well:

```bash
docker compose -f examples/docker-compose.yml down --volumes
```

The initializer is idempotent: rerunning it drops and recreates the demo table
with the same rows and expected aggregates.

## Implementation note

Conceptually, the Knowledge Graph defines what a valid user means, while the
semantic metric defines how the value is calculated. The current execution
path cannot yet translate a KG entity rule into an SQL predicate, so the metric
formula also contains `ytd_gaap >= 10`. The KG remains the business-definition
and event-evidence source; the metric formula remains the executable SQL
contract.

## Troubleshooting

- **Docker is unavailable:** use `examples/init_demo.sh --sqlite-only` on
  macOS/Linux or `.\examples\init_demo.ps1 -SqliteOnly` on Windows.
- **Registration cannot reach DataBridge:** start the platform lifecycle script
  and confirm `http://127.0.0.1:8765/api/health` responds.
- **KG ingestion fails:** verify Host Provider credentials and the Context LLM
  and embedding selections, including the workspace embedding-space contract.
- **PostgreSQL reports that `dws_gaap_di` is missing:** rerun the platform demo
  initializer and check `docker compose -f examples/docker-compose.yml ps`.
- **The analysis returns no March data:** include the explicit March 2026 date
  range in the prompt; the demo is intentionally historical.
