# Advice cards (v1.1)

A non-blocking suggestion card on the dashboard: when the weather warrants it,
the page shows one short, actionable sentence with the number it is based on.

Phase one is **deterministic** — rules and templates, no model, no retrieval,
no external AI service. Phase two swaps the wording generator for a
retrieval-backed one without touching anything else.

## Goal

The dashboard reports what the weather *is*. This adds what a person might
*do* about it — and does so under constraints that matter more than the
feature itself:

- **It must never degrade the weather page.** Advice is requested after the
  weather has rendered, every failure path returns "no card", and the card API
  cannot make `/api/latest` slower or less available.
- **It must be quiet.** Something that pops the same suggestion on every
  refresh gets ignored, then resented. Deduplication, a frequency window and a
  mute are part of the feature, not a later polish pass.
- **It must be explainable.** Every card carries the reading it was derived
  from, and every sentence it can emit is a fixed string a reviewer can read.

## Flow

```
serving snapshot → freshness check → rule engine → dedup + frequency
                 → content provider → card → GET /api/advice → dashboard
                                                             → POST feedback
```

Each step is a separate module under `src/functions/weather/advice/`:

| File | Responsibility |
| --- | --- |
| `models.py` | Card protocol, triggers, severities, `WeatherContext` |
| `rules.py` | Thresholds and priority — pure functions, no wording |
| `providers.py` | `AdviceContentProvider` protocol, `TemplateAdviceProvider` |
| `frequency.py` | Dedup keys, frequency window, mute, expiry — pure functions |
| `repository.py` | Per-session state; in-memory or blob-backed |
| `service.py` | Orchestration and structured logging |

## Rules

Every threshold is configuration (`AdviceSettings`), never a literal in a
conditional.

| Trigger | Fires when | Severity | Priority | Setting |
| --- | --- | --- | --- | --- |
| `EXTREME_HEAT` | `temp_c >= 35` | warning | 10 | `ADVICE_HEAT_C` |
| `HIGH_WIND` | `wind_kph >= 40` | warning | 15 | `ADVICE_WIND_KPH` |
| `HIGH_UV` | `uv >= 8` | warning | 20 | `ADVICE_UV_INDEX` |
| `RAIN_EXPECTED` | next hour `chance_of_rain >= 80` | info | 30 | `ADVICE_RAIN_CHANCE_PERCENT` |
| `NO_RECOMMENDATION` | nothing matched | — | — | — |

Lower priority number wins when several fire. **Priority and severity are
separate on purpose**: a rule can be worth showing first while still only
warranting a quiet presentation, and an official severe-weather warning can
later slot in above everything without every existing rule being re-graded.

A missing reading is never treated as a safe one. `precip_chance_next_hour` of
`null` means "no hourly forecast covered the window", not "it will not rain",
and the rule declines rather than guessing.

### The data this required

`RAIN_EXPECTED` could not be built from what the pipeline collected. The daily
forecast carries `daily_chance_of_rain`, which answers a different question, so
ingestion was extended with a `forecast_hour` record type.

Only the next `WEATHER_FORECAST_HOURS_AHEAD` hours (default 6) are kept. The
upstream response contains every hour of every requested day — 72 records per
location per poll — and at a 30-minute cadence storing all of them would have
quadrupled what lands in bronze to serve a rule that never looks past the next
hour.

## Card protocol

```json
{
  "recommendation_id": "sha256(location|trigger|snapshot|rule_version)[:32]",
  "location": "Tokyo",
  "trigger_code": "RAIN_EXPECTED",
  "severity": "info",
  "title": "一小时内可能下雨",
  "message": "未来一小时降水概率较高，出门记得带伞哦。",
  "evidence": [{ "label": "降水概率", "value": "85%" }],
  "weather_observed_at_utc": "2026-08-04T05:50:00Z",
  "generated_at_utc": "2026-08-04T06:00:00Z",
  "expires_at_utc": "2026-08-04T07:00:00Z",
  "generation_method": "template-v1",
  "weather_snapshot_id": "9f2c…",
  "rule_version": "2026-08-04",
  "actions": [
    { "type": "dismiss", "label": "知道了" },
    { "type": "mute", "label": "今天不再提醒" }
  ]
}
```

**Observation time and generation time are separate fields**, for the same
reason they are separate in the lake: they diverge, and a card that conflates
them cannot be reasoned about after the fact. All timestamps are UTC, ISO-8601,
`Z`-suffixed.

## Freshness, deduplication, frequency

**Freshness.** A card claims to describe the weather *now*, so it refuses to be
built on an observation older than `ADVICE_MAX_WEATHER_AGE_MINUTES` (default
90). The serving layer is curated hourly, so a tighter window would reject
almost everything; this is a property of the pipeline, not a preference.

**Deduplication.** The recommendation id is
`hash(location + trigger + weather_snapshot_id + rule_version)`. The same advice
about the same observation is the same card, and generating it twice produces
the same id — so the dedup is a consequence of determinism rather than a table
lookup. `rule_version` is in the key so that changing a threshold or the copy
*is* allowed to reach people who already saw the previous version.

**Frequency.** One card per trigger per `ADVICE_MIN_INTERVAL_MINUTES` (default
180) per session.

**Mute.** `今天不再提醒` suppresses that trigger for the rest of the UTC day —
the button says "today", so the default honours the wording literally.

**Escalation overrides both.** If the risk level rises (`info` → `warning` →
`severe`), the card is shown again even inside the window or under a mute.
Withholding a more serious message because a milder one was dismissed is the
one failure this policy must not have.

**Expiry.** Cards carry `expires_at_utc` (default 60 minutes) and the front end
stops rendering them past it.

## Why templates, not an LLM, in phase one

1. **Reviewability.** Every sentence the system can emit is in `TEMPLATES` and
   fits on one screen. Nobody has to reason about what it *might* say.
2. **Testability.** The same weather always produces the same words, so the
   card can be asserted exactly. A generated string could only be tested for
   shape.
3. **Cost and latency.** This project already died once to an Azure bill. A
   per-request model call on a page that refreshes every 60 seconds is exactly
   the kind of cost that does not show up until it does.
4. **Separation first.** Getting the rules, the policy and the protocol right
   is the hard part. Wording is the easy part, and doing it last means phase
   two changes one class instead of the whole feature.

## Phase two: adding retrieval

> **Delivered in v1.2.** This section is the plan as written during v1.1,
> kept because the prediction below is what the seam was designed against
> and it is worth being able to check it against what shipped. The built
> thing is documented in **[rag.md](rag.md)**.

Implement the protocol and pass it in. Nothing else changes:

```python
class RagAdviceProvider:
    name = "rag-v1"

    def generate(self, trigger: AdviceTrigger, weather: WeatherContext) -> AdviceContent:
        ...  # retrieve, ground, generate
        return AdviceContent(title=..., message=..., generation_method=self.name)

AdviceService(provider=RagAdviceProvider())
```

What is already in place for it:

- `generation_method` is on every card and every feedback record, so the two
  providers can be compared on `helpful` / `not_helpful` rates.
- The service catches provider exceptions and degrades to "no card", so a model
  outage cannot break the page.
- Evidence comes from the rule, not the provider — the numbers must stay
  identical whichever generator produced the prose, so a hallucinated figure
  cannot reach the card.
- `rule_version` invalidates dedup keys, so switching provider can be made
  visible to users who already saw a templated card.

What phase two still has to decide: where the corpus lives, whether generation
happens per request or is precomputed per `(trigger, location)`, and how output
is validated before it is shown.

How v1.2 answered those three: a reviewed corpus in `knowledge/`, built into a
JSON index that the runtime reads; generation per request, cached on the
weather snapshot id so a poll loop does not re-pay for it; and validation by
deterministic code that resolves every citation against the passages retrieved
in that same request.

## API

```
GET /api/advice?location=Tokyo[&session=<id>]
```

| Status | When |
| --- | --- |
| `200` | A card. `ETag` is the recommendation id; `Cache-Control: private, max-age=60` |
| `204` | Nothing to say: no rule matched, weather stale, suppressed, or the snapshot does not exist yet. `X-Advice-Outcome` says which |
| `400` | `location` missing or not collected by this deployment |

```
POST /api/advice/feedback
```

Body accepts `shown`, `clicked`, `dismissed`, `muted`, `helpful`,
`not_helpful`. Returns `202` — feedback is telemetry and must never fail the
caller. `muted` is the only event that changes future behaviour.

Records carry recommendation id, trigger, location, snapshot id, generation
method, event, timestamp and an anonymous session id. **Nothing else** — no
address, no user agent, no identifier that outlives the browser session.

## Observability

Structured logs, one line per decision, with `trigger`, `location`,
`generation_method`, `rule_version` and `recommendation_id` in custom
dimensions:

`ADVICE_GENERATED` · `ADVICE_NO_RULE_MATCHED` · `ADVICE_STALE_WEATHER` ·
`ADVICE_SUPPRESSED_FREQUENCY` · `ADVICE_SUPPRESSED_MUTED` ·
`ADVICE_PROVIDER_FAILURE` · `ADVICE_DISABLED` · `ADVICE_FEEDBACK`

## Running it locally

No Azure needed. The dashboard falls back to `dashboard/data/advice.json`,
which `scripts/generate_sample_data.py` produces by running the real
`AdviceService` over the sample snapshot — so the offline card is the card the
deployed service would build.

```bash
python scripts/generate_sample_data.py
python scripts/serve_dashboard.py     # http://127.0.0.1:4280
```

```bash
pytest tests/test_advice_rules.py tests/test_advice_service.py tests/test_advice_api.py
```

## Known limits

- Frequency state is per anonymous session, not per person. Clearing site data
  resets it, which is the honest trade for storing nothing identifying.
- The blob-backed repository writes one small document per session and relies
  on the storage lifecycle policy for cleanup; it is not built for high
  concurrency on a single session.
- Advice reads the hourly-curated serving snapshot, so "next hour" is as fresh
  as the last `curate` run. Tightening that means changing the pipeline
  cadence, not the advice engine.
- Only one card is shown at a time, by design. Several matching rules produce
  the highest-priority one.
