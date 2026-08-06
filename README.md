# Weather Streaming Pipeline

*English · [简体中文](README.zh-CN.md)*

A serverless ingestion-and-monitoring pipeline on Azure: telemetry is polled every 30 seconds,
streamed through Event Hubs, landed in a data lake, curated into a queryable table, and surfaced on
a public dashboard — with alerting on both the pipeline and the data flowing through it.

On top of it sits a **card-style advisory feature**: a suggestion that fires on a weather
condition, worded from retrieved official safety guidance, with every recommendation traceable to
the passage it came from. Behind it, always, is a deterministic template — if retrieval or the
model fails in any way, that is what writes the card.

> **You are reading v1.2.** What changed in this version, and why, is in its
> [release notes](https://github.com/Shomao1998/weather-streaming-app/releases/tag/v1.2);
> the full version history is on the
> [releases page](https://github.com/Shomao1998/weather-streaming-app/releases).

| | |
| --- | --- |
| **Stack** | Azure Functions (Python 3.12, Flex Consumption) · Event Hubs · ADLS Gen2 · Application Insights · Static Web Apps · Bicep |
| **Run it locally** | `python scripts/serve_dashboard.py` — the dashboard renders committed sample data, no Azure account needed |
| **Live deployment** | Paused. See below. |

> **The hosted deployment is currently off.** A different project in the same
> subscription — an Azure AI Search Standard instance at roughly \$240/month —
> exhausted the credit, and the subscription went read-only. The weather
> pipeline was about 2% of that bill. It is being restored after the billing
> period resets; the disposal plan is in
> [`docs/deployment.md`](docs/deployment.md).
>
> I would rather say this than leave a link that 404s. Measuring what this
> actually costs, and being wrong about it the first time, turned out to be one
> of the more useful parts of the project — the original README estimated
> \$12–14/month and the measured figure was \$44.

---

## Why this project exists

I worked in consulting, on a project migrating a large financial institution's on-premise data to
the cloud. One requirement was how to store the institution's internal network syslog once it
moved. The initial proposal used Azure storage to hold the logs and Application Insights to detect
transfer anomalies, meeting a **hard zero-loss requirement** and a compliance retention of at least
two years. It did not proceed: log volume was terabytes at minimum, and storage — **even entirely
in the Archive tier** — combined with Application Insights monitoring charges came to far more than
the project could absorb.

Separately on that project I tracked task progress across teams and maintained the dashboard
reporting it. Drawing on other open-source portfolio projects, this one merges the two
requirements into a streaming weather collection and monitoring dashboard.

A third requirement on the same engagement never got built. The migration would leave staff working
in a system they did not know, and the plan was a **card-style chatbot** answering their questions
from the updated internal knowledge base and FAQ — retrieval over a document set that was itself
changing week by week. It was still early-stage planning when I left. The advice card in this
version is the deterministic half of that idea: it fires on a condition and words itself from
reviewed templates, with no model anywhere. Retrieval is the next step, and the seam it plugs into
is already here.

**Build method.** I defined the requirements, constraints and acceptance criteria and made the
scope and cost decisions; the implementation was produced by iterating with an AI coding agent.

**Cost control.** Application Insights sampling is enabled; the default log level is `Warning` (at
`Information` the Azure SDK logs every HTTP request it issues); each storage layer carries a
retention policy; the single high-cost component is toggled by a configuration flag. A technically
sound design can still be unviable on operating cost.

Weather data substitutes for logs because the two share three properties:

- **Reported faster than they change.** The upstream API refreshes every 10–15 minutes against a
  30-second poll, so the stream is mostly duplicates — equivalent to a device re-emitting an
  unchanged status line.
- **Records are not equally important.** A reading above 38 °C corresponds to a `CRITICAL` log
  line: it must trigger a response, not merely land in storage.
- **Absence is the real failure.** A pipeline that has stopped ingesting is externally
  indistinguishable from a healthy one unless something monitors for the absence of data.

## Architecture

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/images/architecture-dark.svg">
    <img src="docs/images/architecture-light.svg" alt="Pipeline architecture: weatherapi.com feeds two timer functions into Event Hubs; archive_to_bronze drains the stream into a bronze layer; curate runs hourly into silver Parquet and serving JSON; serving is exposed through an HTTP API to a Static Web App dashboard, silver goes to Power BI; threshold breaches flow to Application Insights and on to Azure Monitor alert rules. Below, api_advice reads the same serving snapshot the dashboard does and returns a rule-driven card to it." width="560">
  </picture>
</p>

<sub>Diagram source: <a href="scripts/render_architecture.py"><code>scripts/render_architecture.py</code></a>. Product icons are Microsoft's official Azure architecture icons, used unmodified under the terms Microsoft publishes with them; weatherapi.com is drawn as a plain shape because it is not a Microsoft product.</sub>

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

**A buffer in front of storage, and an append-only raw layer.** Ingestion writes to Event Hubs
rather than to storage directly, and bronze is only ever appended to. Both follow from the
zero-loss requirement: a downstream failure fills a buffer instead of dropping records, and nothing
already landed is ever rewritten.

**Splitting ingestion by how fast the data actually changes.** The original polled three endpoints
every 30 seconds. Forecasts and weather alerts change a few times a day; polling them at observation
frequency wasted roughly 90% of the API quota for identical bytes. Current conditions stay on the
30-second timer; forecast and alerts moved to 30 minutes and share a single request.

**Deterministic record ids instead of stateful de-duplication.** Each record's id is a hash of
`(location, upstream observation timestamp)`. Polling faster than the source refreshes produces the
same id, so the curation step collapses duplicates with a dictionary and no state store, no
watermark table, and no exactly-once delivery requirement on the stream. This is the zero-loss
requirement resolved in favour of duplication: losing a record is unacceptable, repeating one is
merely collapsed later.

**Observation time and ingestion time are separate fields.** They diverge — by the poll interval
normally, by much more during an outage and replay. Collapsing them into one column makes
late-arriving data impossible to reason about after the fact — and replay after an outage is
precisely what a zero-loss guarantee makes routine.

**A consumer function instead of Event Hubs Capture.** Capture is the managed way to land a stream
in storage, but it is billed per throughput unit per hour and writes Avro. A ~40-line Event Hub
triggered function costs effectively nothing at this volume, writes JSONL that is readable without
tooling, and is itself part of the portfolio. The cost argument is the same one that stopped the
original proposal: per-unit managed billing is what scales badly.

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

## Advice cards

The dashboard shows one short, actionable suggestion when the weather warrants
it — "take an umbrella", "drink water" — with the reading it was derived from.

v1.1 is deterministic: rules and templates, **no model and no retrieval**.
Every sentence the system can emit is a fixed string, so the same weather always
produces the same words and the card can be asserted exactly in a test.
`providers.AdviceContentProvider` is the seam a retrieval-backed generator slots
into later without the rules, the frequency policy or the card protocol
changing.

| Trigger | Fires when | Priority |
| --- | --- | --- |
| `EXTREME_HEAT` | `temp_c >= 35` | 1 |
| `HIGH_WIND` | `wind_kph >= 40` | 2 |
| `HIGH_UV` | `uv >= 8` | 3 |
| `RAIN_EXPECTED` | next hour `chance_of_rain >= 80` | 4 |

Advice is strictly secondary: it is requested only after the weather has
rendered, and every failure — stale data, a suppressed card, a broken provider,
an unreachable API — resolves to showing nothing rather than to a worse page.

Deduplication is a consequence of determinism: the recommendation id is
`hash(location + trigger + weather_snapshot_id + rule_version)`, so the same
advice about the same observation is literally the same card. A rising risk
level overrides both the frequency window and a mute.

Full design and API contract: **[docs/advice.md](docs/advice.md)**.

### v1.2 — grounded in official guidance

v1.2 replaces the templates with a model that is handed the live weather
facts plus retrieved passages from a small corpus of official safety guidance,
and must cite the passage every recommendation came from.

The rules did not move. Whether a card appears, which hazard it is about, how
severe it is, when it is suppressed and when it expires are all still decided by
the deterministic engine above. Retrieval and generation only choose words —
and if anything about them fails, the v1.1 template writes the card
instead.

| | |
| --- | --- |
| Corpus | 6 registered sources (5 live, 1 deliberately retired), 23 chunks, US federal public domain |
| Ingestion | content-addressed chunk ids — re-running reproduces the index byte for byte |
| Retrieval | hybrid BM25 + vector, RRF K=60, hazard/jurisdiction/`enabled` filters applied structurally |
| Validation | deterministic only — citations must resolve to *this* retrieval, actions come from a closed 19-code vocabulary, numbers must appear in the weather facts or a cited passage |
| Fallback | every failure path returns a v1.1 card |
| Evals | 53 cases, run in CI; 0 unresolvable citations, 0 hazard leaks, 0 missed fallbacks |

Retrieval is an interface with two implementations: Azure AI Search for
production, and a local index that runs the same strategy at zero cost — which
is what makes the retrieval layer executable in CI, and what kept this feature
buildable while the subscription was disabled. **The Azure path is written and
its checkable parts are asserted in tests, but it has never talked to a live
service.**

Full design, evaluation results and limitations: **[docs/rag.md](docs/rag.md)**.

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
knowledge/                        reviewed source registry, raw documents, built index
evals/                            53 retrieval and generation cases, run as a CI gate
scripts/                          ingestion, index build, architecture render, sample data, OIDC
tests/                            329 tests
docs/architecture.md              deeper rationale, cost, alternatives considered
docs/advice.md                    advice rules, card protocol (v1.1)
docs/rag.md                       knowledge base, retrieval, grounding, evaluation
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

I estimated this at **$12–14 a month**. Measured, it was about **$44** — a
three-times miss, and worth writing down rather than quietly correcting.

| Resource | Estimated | Why the estimate was wrong |
| --- | --- | --- |
| Event Hubs Basic | ~$11 | Correct. |
| Function App (Flex Consumption) | ~$1–2 at ~90k executions | Flex has **no free execution grant**. Consumption (Y1) does, and I carried that assumption over — but Y1 could not be deployed at all on this subscription's zero VM quota. |
| Storage (ADLS Gen2 + runtime) | <$1 | Roughly correct. |
| Log Analytics / App Insights | $0 within the 5 GB free grant | Billed **per GB ingested**, and a 30-second timer writing at `Information` level ingests more than expected. Lowering the default log level to `Warning` was as much a cost fix as a noise fix. |
| Azure Monitor alert rules | not counted | ~$1 per rule per month, three rules. |
| Static Web Apps | $0 (Free tier) | Correct. |

The line items above are the original estimate; a corrected table is pending a
full week of clean billing data after the subscription is restored. The lesson
generalises past this project: the free grants that make a serverless estimate
look cheap are attached to *specific SKUs*, and a platform constraint that
forces a different SKU can quietly delete them.

Event Hubs is the largest single line and the only component that is strictly optional — the sink
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
