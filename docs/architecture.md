# Architecture notes

Rationale that would clutter the README, kept where it can be read on purpose.

## The duplicate problem, and why it shapes everything

The upstream API refreshes an observation every 10–15 minutes. The poller runs every 30 seconds.
That is deliberate — the brief was to reproduce a 30-second collection cadence — but it means
roughly **95% of what enters the stream is a repeat of what is already there**.

Three ways to handle that:

1. **Poll slower.** Correct for weather, wrong for the exercise: the point is a high-frequency
   collector, and real log shippers do not get to choose their emission rate.
2. **De-duplicate at ingest.** Requires the collector to remember what it last sent, per location.
   State in the hot path, and wrong under horizontal scale — two instances would each keep their own
   memory.
3. **De-duplicate at rest, with a deterministic key.** Chosen. `record_id = sha256(location,
   upstream_observed_at)[:32]`. Repeats collapse in a dictionary during curation. The collector
   stays stateless, the stream stays honest about what was actually received, and bronze keeps the
   full record of what arrived and when — which is what you want when debugging a gap.

The cost is storage: bronze holds ~2,880 records per location per day, of which ~96 are distinct.
At roughly 1 KB each that is under 100 MB a month for three locations — far cheaper than the
complexity of the alternatives.

## Why observation time and ingestion time are separate columns

`observed_at_utc` comes from the upstream `last_updated_epoch`. `ingested_at_utc` is when this
system saw it. In steady state they differ by less than the poll interval, so it is tempting to keep
one.

They diverge exactly when it matters. During an outage and catch-up, a batch of records lands with
recent ingestion times and old observation times. If only one timestamp exists, the choice is
between a chart that shows a spike of readings that never happened at that moment, or a lake in
which "when did we receive this?" is unanswerable. Two columns cost eight bytes.

This is the same reason log pipelines separate event time from collection time, and the reason
watermarking exists in stream processors.

## Plan selection

| Option | Outcome |
| --- | --- |
| `Y1` Consumption | **Rejected by Azure.** `SubscriptionIsOverQuotaForSku`, VM quota 0, in all four regions tried |
| `B1` Basic App Service | Same quota failure |
| `FC1` Flex Consumption | Works |

Visual Studio subscriptions ship with no VM quota. `Y1` is serverless from the user's perspective
but still allocates against that pool; Flex Consumption does not.

Having been forced there, Flex is the better fit anyway: faster cold starts, per-instance-memory
billing, and a maximum instance count that caps runaway spend. The one thing lost is the classic
plan's free monthly grant of 1M executions — at ~90k executions a month the difference is a couple
of dollars.

## Identity and the deployment bootstrap

Flex Consumption reads its deployment package from a blob container **at host start**. With a
system-assigned identity the ordering is impossible: the principal does not exist until the app is
created, but the app needs storage access the moment it starts.

A user-assigned identity is created first, granted its roles, and then attached to the app. The
template grants:

| Scope | Role | Used for |
| --- | --- | --- |
| Runtime storage | Storage Blob Data Owner | Deployment package, timer singleton locks, Event Hub checkpoints |
| Runtime storage | Storage Queue / Table Data Contributor | Host bookkeeping |
| Event hub | Azure Event Hubs Data Sender | The two ingest functions |
| Event hub | Azure Event Hubs Data Receiver | The archive function |
| Lake storage | Storage Blob Data Contributor | bronze / silver / serving writes |
| Key Vault | Key Vault Secrets User | The weather API key |

`AZURE_CLIENT_ID` in the app settings tells `DefaultAzureCredential` in application code which
identity to present; `EVENT_HUB_CONNECTION__clientId` does the same for the trigger binding.

Result: no connection strings, no account keys, no Key Vault access policies, nothing to rotate.

## Event Hubs Capture versus a consumer function

Capture is the managed answer and needs no code. It was rejected on two grounds.

**Cost.** Capture is billed per throughput unit per hour on top of the namespace, which at the time
of writing more than triples the monthly bill for a pipeline whose entire point is to be cheap
enough to leave running.

**Format.** Capture writes Avro with its own envelope schema. Reading it means either a library or a
Spark session. The archive function writes newline-delimited JSON that `cat` can read and any tool
can parse, into partitions chosen by this project rather than by the service.

The function is ~40 lines including error handling. At this volume it costs cents.

## Serving the dashboard

The dashboard needs three small documents. Four ways to get them into a browser:

1. **Anonymous blob access on the serving container.** Simplest, and rejected: public access is an
   account-level switch on Azure Storage, so enabling it for `serving` exposes `bronze` too unless
   every container's access level is managed correctly forever.
2. **SAS tokens.** They expire, so either the page ships an expiring URL or something must mint
   them — which is a backend, which is option 3.
3. **HTTP endpoints on the Function App.** Chosen. Three anonymous read-only routes with a
   30-second in-process cache. The lake stays private, the cache means an open browser tab does not
   generate one storage transaction per poll per viewer, and the endpoints are the natural place to
   put a schema boundary between storage layout and page.
4. **Static Web Apps linked backend.** Would remove the cross-origin call, but requires the Standard
   tier.

## Aggregation happens once, not per viewer

`curate` computes `latest.json`, `timeseries_24h.json` and `breaches_24h.json` hourly and writes
them as complete documents. The dashboard does no aggregation: it fetches three files totalling
~50 KB and draws them.

The alternative — query the lake per page load — would put a query engine in the request path for a
page whose data changes hourly. Pre-aggregation is why the whole dashboard is three files and no
framework.

## What I would change at higher volume

- **Watermarked curation.** Reprocessing a rolling 24 hours is cheap at 300 records an hour and
  wasteful at 300 a second. A checkpoint blob recording the last processed bronze partition would
  make the cost proportional to new data.
- **Compaction.** The silver layer rewrites whole-day Parquet files. Incremental row groups plus a
  periodic compaction job would scale further, at the cost of a small-file problem to manage.
- **Event Hubs Standard.** Basic allows one consumer group, so a second independent consumer — a
  real-time alerting path separate from archival — needs the Standard tier.
- **Schema registry.** `schema_version` on every record is the hook; today the contract is enforced
  by tests rather than by a registry.
