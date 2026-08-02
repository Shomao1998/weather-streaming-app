# Weather Streaming Pipeline

*English · [简体中文](README.zh-CN.md)*

A serverless ingestion-and-monitoring pipeline on Azure: telemetry is polled every 30 seconds,
streamed through Event Hubs, landed in a data lake, curated into a queryable table, and surfaced on
a public dashboard — with alerting on both the pipeline and the data flowing through it.

| | |
| --- | --- |
| **Live dashboard** | https://lively-pond-063e00c0f.7.azurestaticapps.net |
| **Health endpoint** | https://func-weather-e5lpvy.azurewebsites.net/health |
| **Live API** | [`/api/latest`](https://func-weather-e5lpvy.azurewebsites.net/api/latest) · [`/api/timeseries`](https://func-weather-e5lpvy.azurewebsites.net/api/timeseries) · [`/api/breaches`](https://func-weather-e5lpvy.azurewebsites.net/api/breaches) |
| **Stack** | Azure Functions (Python 3.12, Flex Consumption) · Event Hubs · ADLS Gen2 · Application Insights · Static Web Apps · Bicep |

---

## Why this project exists

I worked on a requirement for storing network syslog after a cloud migration. The initial proposal
used Azure storage to hold the logs and Application Insights to detect transfer anomalies, meeting
a zero-loss requirement and a compliance retention of at least two years; it did not proceed,
because storage and monitoring costs at terabyte scale were prohibitive. Separately on that project
I tracked task progress across teams and maintained the dashboard reporting it. Taking cues from
public portfolio projects, this one merges the two requirements into a streaming weather collection
and monitoring dashboard.

**How it was built.** I defined the requirements, constraints and acceptance criteria and made the
scope and cost decisions; the implementation was produced by iterating with an AI coding agent.

That history shapes how cost is handled here: Application Insights sampling is enabled, the default
log level is `Warning` (at `Information` the Azure SDK logs every HTTP request it issues), every
storage layer carries an explicit retention policy, and the single expensive component sits behind
a configuration flag. A technically sound design can still fail on operating cost.

The substitution of weather for logs is deliberate rather than cosmetic. Weather readings share the
properties that make log ingestion awkward:

- **They arrive faster than they change.** The upstream API refreshes every 10–15 minutes while the
  poller runs every 30 seconds, so the stream is mostly duplicates — the same problem as a device
  re-emitting an unchanged status line.
- **Some records matter more than others.** A temperature crossing 38 °C is the analogue of a
  `CRITICAL` log line: it has to trigger something, not just land in storage.
- **Gaps are the real failure.** A pipeline that ingests nothing looks identical to a healthy one
  from the outside unless something is explicitly watching for silence.

## Architecture

```mermaid
flowchart LR
    API[weatherapi.com]

    subgraph ingest["Ingestion"]
        C["ingest_current<br/>timer · 30s"]
        F["ingest_forecast<br/>timer · 30min"]
    end

    EH[["Event Hubs<br/>weather-events"]]
    AR["archive_to_bronze<br/>Event Hub trigger"]

    subgraph lake["ADLS Gen2"]
        B[("bronze<br/>raw JSONL")]
        S[("silver<br/>Parquet")]
        SV[("serving<br/>aggregated JSON")]
    end

    CU["curate<br/>timer · hourly"]
    HTTP["HTTP API<br/>/api/latest · /api/timeseries"]
    DASH["Static Web App<br/>public dashboard"]
    PBI["Power BI"]

    AI["Application Insights"]
    ALERT["Azure Monitor<br/>alert rules"]

    API --> C & F
    C & F --> EH
    EH --> AR --> B
    B --> CU
    CU --> S & SV
    SV --> HTTP --> DASH
    S --> PBI
    C -.threshold breaches.-> AI
    ingest -.telemetry.-> AI
    AI --> ALERT
```

### The five functions

| Function | Trigger | Responsibility |
| --- | --- | --- |
| `ingest_current` | Timer, 30s | Poll current conditions and air quality; evaluate thresholds |
| `ingest_forecast` | Timer, 30min | Poll the daily forecast and active alerts in one combined request |
| `archive_to_bronze` | Event Hub | Drain the stream into partitioned raw JSONL |
| `curate` | Timer, hourly | bronze → silver Parquet and the serving documents |
| `health`, `api_*` | HTTP | Liveness probe and the dashboard's read-only data API |

### Storage layout

```
bronze/  current/date=2026-08-01/hour=14/20260801T143005-a1b2c3d4.jsonl   append-only, never rewritten
silver/  current/date=2026-08-01/current-20260801T150000.parquet          de-duplicated, flat columns
serving/ latest.json · timeseries_24h.json · breaches_24h.json           small, pre-aggregated
```

Hive-style partitions (`date=`, `hour=`) are directories on a hierarchical-namespace account, so
Power BI, Spark, Fabric and DuckDB can all prune partitions without extra configuration.

## Design decisions

**Splitting ingestion by how fast the data actually changes.** The original polled three endpoints
every 30 seconds. Forecasts and weather alerts change a few times a day; polling them at observation
frequency wasted roughly 90% of the API quota for identical bytes. Current conditions stay on the
30-second timer; forecast and alerts moved to 30 minutes and share a single request.

**Deterministic record ids instead of stateful de-duplication.** Each record's id is a hash of
`(location, upstream observation timestamp)`. Polling faster than the source refreshes produces the
same id, so the curation step collapses duplicates with a dictionary and no state store, no
watermark table, and no exactly-once delivery requirement on the stream.

**Observation time and ingestion time are separate fields.** They diverge — by the poll interval
normally, by much more during an outage and replay. Collapsing them into one column makes
late-arriving data impossible to reason about after the fact.

**A consumer function instead of Event Hubs Capture.** Capture is the managed way to land a stream
in storage, but it is billed per throughput unit per hour and writes Avro. A ~40-line Event Hub
triggered function costs effectively nothing at this volume, writes JSONL that is readable without
tooling, and is itself part of the portfolio.

**The lake is never publicly readable.** Serving the dashboard directly from blob storage would mean
enabling anonymous access at the account level, which exposes the raw bronze data too. The Function
App instead exposes three anonymous read-only endpoints with a 30-second in-process cache, so an
open browser tab does not become one storage transaction per poll per viewer.

**Flex Consumption, not the classic Consumption plan.** Not a preference: `Y1` and every App Service
tier fail preflight on a Visual Studio subscription with `SubscriptionIsOverQuotaForSku` — those
SKUs draw on a VM quota that is zero. Flex Consumption uses a different pool. It also cold-starts
faster and scales on instance memory rather than VM count.

**A user-assigned managed identity.** Flex Consumption reads its deployment package from blob
storage at first start, which a system-assigned identity cannot authorise, because it does not exist
until the app does. Creating the identity first and granting its roles up front removes the ordering
problem — and as a side effect there is no connection string or account key anywhere in the
configuration. Event Hubs, Storage and Key Vault are all reached by identity.

## Monitoring

Three alert rules, covering three genuinely different failure modes:

| Alert | Condition | Why it exists |
| --- | --- | --- |
| No ingest | No successful run in 15 minutes | A stalled pipeline looks healthy from outside |
| Failures | More than 5 failed invocations in 15 minutes | Upstream outage, expired key, bad deployment |
| Threshold breach | A `critical` reading in the last 10 minutes | The business-level alert — a `CRITICAL` log line |

Breaches are emitted as structured logs with custom dimensions; Application Insights ingests them
and the alert rules query them. Logging is the alerting transport, so no extra service is involved.

## Repository layout

```
infra/main.bicep                  every Azure resource, idempotent, one command
src/functions/                    deployment package — host.json at its root
  function_app.py                 trigger registration only
  weather/                        config · api · models · transform · monitoring
                                  clients · sinks · pipeline · serving
dashboard/                        three files, no framework, no external requests
scripts/                          local dashboard server, sample data, OIDC setup
tests/                            92 tests
docs/architecture.md              deeper rationale, cost, alternatives considered
docs/deployment.md                runbook, first-deploy checklist, troubleshooting
powerbi/                          report template and connection notes
```

## Running it locally

Nothing here needs an Azure subscription.

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

The dashboard renders against committed sample data:

```bash
python scripts/serve_dashboard.py   # http://127.0.0.1:4280
```

To run the functions against the real API, copy the settings template, add a
[weatherapi.com](https://www.weatherapi.com/) key, and start the host:

```bash
cp src/functions/local.settings.json.example src/functions/local.settings.json
# set WEATHER_API_KEY, leave EVENT_HUB_ENABLED false to write straight to Azurite
cd src/functions && func start
```

## Deploying

```bash
az deployment group create \
  --resource-group rg-weather-streaming \
  --template-file infra/main.bicep \
  --parameters infra/main.parameters.json
```

Then push to `main`, or run the **Deploy** workflow. See [docs/deployment.md](docs/deployment.md)
for the first-deployment checklist, the Key Vault secret, and CI authentication setup.

## Cost

Roughly **$12–14 a month**, dominated by one line item:

| Resource | Monthly |
| --- | --- |
| Event Hubs Basic | ~$11 |
| Function App (Flex Consumption) | ~$1–2 at ~90k executions |
| Storage (ADLS Gen2 + runtime) | <$1 |
| Log Analytics / App Insights | $0 within the 5 GB free grant |
| Static Web Apps | $0 (Free tier) |

Event Hubs is the only meaningful cost and the only component that is strictly optional — the sink
layer is an interface, and `EVENT_HUB_ENABLED=false` makes ingestion write straight to bronze.

### Retention

Storage that only ever grows is the main reason a log platform becomes constrained by cost rather
than by architecture. Each layer's policy is declared in `infra/main.bicep`:

| Layer | Policy |
| --- | --- |
| Event Hubs | 1 day — a buffer, not a store; the archive function drains it continuously |
| `bronze` | Cool at 30 days → Archive at 90 → deleted at 730 (a two-year compliance retention) |
| `silver` | Cool at 90 days; never archived, never deleted, and rebuildable from bronze |
| `serving` | always Hot — three small files, rewritten hourly and read on every page load |
| Log Analytics | 30 days |

Archiving bronze is safe precisely because nothing downstream reads it: `curate` only ever touches
the last 24 hours, so raw data is cold within a day of landing. Retrieving it later costs a
rehydration wait, which is the right trade for data kept to satisfy an auditor rather than a query.

## Limitations

- Event Hubs Basic retains one day and allows a single consumer group. Fine here, because the
  archive function drains continuously; a second consumer would need the Standard tier.
- `curate` reprocesses a rolling 24 hours each run rather than tracking a watermark. At this volume
  that is cheaper than the bookkeeping; at 100× it would not be.
- The silver layer rewrites whole-day files instead of compacting incrementally.
- Power BI refreshes on its own schedule and is not real-time; the web dashboard covers the live
  view. See [powerbi/README.md](powerbi/README.md) for why the two are split.
