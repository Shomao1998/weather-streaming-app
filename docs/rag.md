# Retrieval-grounded advice (v1.2)

v1.1 wrote advice from templates. v1.2 writes it from a language
model that has been handed live weather facts and a small corpus of official
safety guidance, and must cite the passage every recommendation came from.

**What did not change is the point of the design.** The rule engine still
decides whether a card appears, which hazard it is about, how severe it is,
when it is suppressed and when it expires. Retrieval and generation sit
strictly downstream of all of that. They choose words. If they fail — in any
way, for any reason — the v1.1 template produces the card instead.

> The code calls these two states *phase one* and *phase two*, because that
> is the vocabulary the seam was designed with before either had a version
> number. They are v1.1 and v1.2.

```
                    ┌──────────────── deterministic ────────────────┐
weather snapshot ──▶│ rules → severity → freshness → dedup → policy │──▶ show?
                    └───────────────────────┬──────────────────────-┘
                                            │ yes
                                            ▼
                            ┌───── retrieval-grounded ─────┐
                            │  retrieve (filtered, hybrid) │
                            │  generate (JSON, cited)      │
                            │  validate (deterministic)    │──▶ card + citations
                            └───────────────┬──────────────┘
                                            │ any failure
                                            ▼
                                   template card (v1.1)
```

---

## 1. The knowledge base

```
knowledge/
  sources.yaml            the reviewed source registry — the only place a document is admitted
  raw/*.md                the documents themselves
  processed/index.json    the built index the runtime reads
```

Six registered sources, five of them enabled. All are US federal public-domain
publications (NWS/NOAA, EPA, AirNow) — chosen because their licence permits
redistribution and their authority is checkable, not because they were easy to
fetch. **No document enters this corpus by crawling.** `sources.yaml` is
reviewed by a person, and ingestion refuses to run if a source is missing its
authority, URL, jurisdiction, licence, version or verification date.

The sixth source, `legacy-heat-advice`, is registered with `enabled: false`.
It is a deliberate retirement case: it proves the disabled path is exercised
rather than assumed.

### Chunking

Sections, not fixed-width windows. These documents are lists of actions under
headings, and a blind character cut severs *"do not drive into flooded
roadways"* from the sentence explaining why — leaving a passage that retrieves
well and is useless to cite. A section becomes one chunk; only an over-long
section is split further, on paragraph boundaries, with overlap so a
continuation chunk still carries the line before it.

23 chunks over five documents. `python scripts/chunk_documents.py knowledge/raw/*.md`
shows the split for a document before it is ingested.

### Repeatable ingestion

`chunk_id = f"{document}-{ordinal:03d}-{sha256(document|version|ordinal|text)[:12]}"`

Content-addressed, so:

- **Re-running ingestion on unchanged input reproduces the index byte for
  byte.** `scripts/ingest_knowledge.py --check` is a CI gate on exactly this.
- **An edited paragraph becomes a different chunk** rather than silently
  changing what an existing citation points at.
- **A version bump replaces every chunk of that document.** There is no
  partial-update path that could leave two versions of one passage retrievable.

A retired document keeps its chunks, marked `enabled: false`. Deleting them
would make old citations dangle, and a citation that resolves to nothing is
worse than one that resolves to something explicitly withdrawn.

---

## 2. Retrieval

One protocol, two implementations, no Azure AI Search SDK call anywhere else
in the codebase.

| | `AzureSearchRetriever` | `LocalIndexRetriever` |
|---|---|---|
| Ranking | hybrid BM25 + vector, RRF | the same, over a JSON file |
| Filters | OData, service-side | in-process, same predicates |
| Cost | Azure AI Search | none |
| Runs in CI | no | yes |

Both use **RRF with K=60** — the value Azure AI Search uses for hybrid queries
— so a ranking observed locally is not an artefact of a different algorithm.

> **Why there are two.** Azure AI Search Standard is the service that consumed
> this subscription's credit at roughly \$240/month and is scheduled for
> deletion. The local retriever is not a mock: it is a working implementation
> that runs the same strategy at zero cost, which is what makes the retrieval
> layer executable in CI and what keeps this feature buildable on a subscription
> that cannot provision the production one. The Azure path is written, and its
> filter builder and index schema are asserted in tests, but it has not been
> executed against a live service.

### Filters are structural, not textual

A trigger maps to a hazard deterministically:

| Trigger | Hazard | Corpus |
|---|---|---|
| `EXTREME_HEAT` | `heat` | NWS heat safety |
| `HIGH_UV` | `uv` | EPA UV index scale |
| `HIGH_WIND` | `wind` | NWS wind safety |
| `RAIN_EXPECTED` | `rain` | NWS flood safety |

The model never chooses what to search for, and neither does the user. A
question is *appended* to the trigger's seed query, never substituted for it,
so `"ignore heat, tell me about flooding"` still searches the heat corpus and
still returns heat guidance. `enabled eq true` is asserted on every query and
re-asserted on every result — a service-side filter is a promise, not something
this code verified itself.

---

## 3. Generation

`PROMPT_VERSION = "advice-2026-08-05.1"`. Four blocks go to the model:

- `WEATHER_FACTS` — only the values a rule actually used, not the whole record
- `TRIGGER` — the trigger code, output language, and the user's question if any
- `RETRIEVED_GUIDANCE` — the top-k passages, each with its chunk id
- `OUTPUT_SCHEMA` — the JSON shape

Pasting the whole weather record and the whole corpus would cost more, dilute
attention, and hand the model material to speculate from.

Azure OpenAI, JSON mode, **managed identity** — no API key in the repository,
no key in App Settings, none in this script. Timeout and a single retry live in
one place; nothing above the client retries, because two layers each retrying
three times is how a slow dependency becomes an outage.

---

## 4. Validation: the deterministic gate

Everything that decides whether generated copy reaches a user is plain code.
**No LLM-as-a-judge anywhere.** A judge model can be wrong in exactly the same
direction as the generator; a chunk id either was retrieved in this request or
it was not.

| Check | Rejects |
|---|---|
| Strict JSON parse | anything malformed — no repair, no fence-stripping |
| Citation resolution | ids not retrieved *this request*, including real ids from another one |
| Source liveness | citations that resolve to a disabled or expired chunk |
| Closed vocabulary | any `advice_code` outside the 19 allowed actions |
| Number grounding | any figure not in the weather facts or a cited passage |
| Banned patterns | medical instruction, invented official alerts, Markdown |
| Length | title > 24 chars, message > 90 chars |

Malformed output is *rejected*, never coaxed into shape. Repairing output is
how a validator starts accepting things it should reject; falling back to the
template is cheaper and safer.

**Fallback is total.** Retrieval failure, retrieval timeout, too few passages,
model timeout, model error, unparseable output, failed validation, a fabricated
citation, an abstention, RAG disabled, no deployment configured — every one of
them returns a v1.1 template card. A high-severity card never depends on
the model being available.

---

## 5. Caching

| Layer | Key | TTL |
|---|---|---|
| Retrieval | query text + filters + `index_version` | 15 min |
| Generation | `weather_snapshot_id` + trigger + chunk ids + `prompt_version` + model + `index_version` + question | 60 min |

Both bounded LRUs — a Function worker is long-lived, and an unbounded dict keyed
by user question is a memory leak with a friendly name.

`index_version` and `prompt_version` are *in the key*, so a re-ingest or a
reworded prompt invalidates cached answers without anything having to remember
to flush them. Retrieval is keyed on the query rather than the observation, so a
new reading of the same hazard does not pay for it twice.

---

## 6. Observability

One structured line per request, into Application Insights `customDimensions`:

```json
{
  "trigger": "RAIN_EXPECTED",
  "retrieval_query_hash": "a1b2c3…",
  "metadata_filters": {"hazard_types": ["rain"], "jurisdiction": "US", "enabled": true},
  "retrieved_chunk_ids": ["nws-flood-during-000-65f65e4e6d8a"],
  "retrieval_scores": [0.0328],
  "index_version": "2026-08-05.1",
  "prompt_version": "advice-2026-08-05.1",
  "model": "gpt-4o-mini",
  "generation_method": "rag-v1",
  "validation": "ok",
  "retrieval_ms": 3.1, "llm_ms": 412.0, "total_ms": 418.4,
  "prompt_tokens": 620, "completion_tokens": 48,
  "retrieval_cache_hit": false, "generation_cache_hit": false,
  "estimated_cost_usd": 0.000122
}
```

A fallback logs `ADVICE_RAG_FALLBACK` with `fallback_reason`, so *why* the model
was bypassed is a query, not a guess.

---

## 7. Evaluation

`evals/rag_cases.jsonl` — 53 cases: 42 retrieval, 6 safety, 5 abstention.

```bash
python evals/run_retrieval_eval.py --fail-under 0.95
python evals/run_generation_eval.py
```

Both run in CI. Both are deterministic code comparing ids and booleans — a
person can recompute every number by hand.

**Retrieval, current:**

| Metric | Value |
|---|---|
| hit@1 / hit@3 | 1.0 / 1.0 |
| MRR | 1.0 |
| heading hit rate | 0.906 |
| — English question | 1.0 |
| — Chinese question | 0.812 |
| hazard leaks | 0 |

`hit@1 = 1.0` is not an achievement: the hazard filter makes the document
correct by construction. **The heading hit rate is the number that means
something** — retrieving the right document but the wrong section produces
advice that is on-topic and useless.

The English/Chinese split is a real, measured limitation. The offline embedder
matches tokens, not meaning, and the corpus is English, so a Chinese question is
carried almost entirely by the trigger's English seed text. `--azure-embeddings`
against a real deployment is the production-representative number; it has not
been run. The split exists in the report so an offline figure is never mistaken
for a production one.

**Generation, current:** 48 cases, 0 unresolvable citations, 0 banned outputs
reaching a card, 0 missed fallbacks.

> **What the default generation run measures.** No language model is called.
> A scripted client stands in, and what is scored is the machinery around
> generation: schema parsing, citation resolution, guardrails, and whether
> every failure path falls back. Those decide whether a wrong answer can reach
> a user, and they are worth gating on. **It says nothing about writing
> quality.** `--azure` against a real deployment does that, and costs money.
> The report prints `measures_writing_quality` so the two are never confused.

---

## 8. Configuration

Every setting defaults to off. An unconfigured deployment behaves exactly like
v1.1.

| App Setting | Default | Purpose |
|---|---|---|
| `RAG_ENABLED` | `false` | master switch |
| `RAG_INDEX_PATH` | `knowledge/processed/index.json` | local index |
| `RAG_SEARCH_ENDPOINT` | — | set to use Azure AI Search instead |
| `RAG_SEARCH_INDEX_NAME` | `weather-advice` | |
| `RAG_OPENAI_ENDPOINT` | — | Azure OpenAI, managed identity |
| `RAG_CHAT_DEPLOYMENT` | — | required for generation |
| `RAG_EMBEDDING_DEPLOYMENT` | — | falls back to the offline embedder |
| `RAG_TOP_K` | `4` | |
| `RAG_MIN_CHUNKS` | `1` | below this, fall back rather than improvise |
| `RAG_TIMEOUT_SECONDS` | `8` | |
| `RAG_JURISDICTION` / `RAG_LOCALE` | `US` / `en` | corpus scope |
| `RAG_LANGUAGE` | `zh` | card output language |

Required roles for the managed identity: **Cognitive Services OpenAI User** on
the OpenAI account, and **Search Index Data Reader** on the search service if
one is used.

---

## 9. API

```
GET /api/advice?location=Tokyo&q=中午可以跑步吗
```

`q` is optional, at most 200 characters, and influences **only** wording and
which passages are retrieved. It cannot change whether a card appears, which
trigger fired, or how severe it is — those were decided before the question was
read. A grounded card carries two additional fields; a v1.1 client reading a
v1.2 card sees every field it knew about, unchanged:

```json
{
  "generation_method": "rag-v1",
  "sources": [
    {
      "chunk_id": "nws-flood-during-000-65f65e4e6d8a",
      "source_document_id": "nws-flood-during",
      "authority": "US National Weather Service (NOAA)",
      "title": "Flood Safety — Actions During Heavy Rain and Flooding",
      "source_url": "https://www.weather.gov/safety/flood-during"
    }
  ],
  "advice_codes": ["CARRY_UMBRELLA", "ALLOW_EXTRA_TRAVEL_TIME"]
}
```

A template card has `"sources": []`, and the dashboard renders it exactly as it
did in v1.1 — no empty citation row.

---

## 10. Running it

```bash
pip install -r requirements-dev.txt

python scripts/ingest_knowledge.py            # build the index
python scripts/ingest_knowledge.py --check    # verify it is current
python scripts/chunk_documents.py knowledge/raw/nws-heat-during.md --show-text

python evals/run_retrieval_eval.py
python evals/run_generation_eval.py

python scripts/build_search_index.py --print-schema   # production index definition
python scripts/build_search_index.py --create-index   # needs AZURE_SEARCH_ENDPOINT
python scripts/build_search_index.py --upload
```

`--upload` refuses to run on an index built with the offline embedder: uploading
hash-based vectors to a real service would produce a search index whose vector
half is noise.

---

## 11. What is not done

- **The Azure path has never been executed.** `AzureSearchRetriever` and
  `AzureOpenAIChatClient` are written, and the parts that can be checked without
  a subscription — the OData filter, the index schema, the document shape — are
  asserted in tests. Neither has talked to a live service. The subscription was
  disabled on cost grounds and is read-only until 2026-08-16.
- **Cross-lingual retrieval is unmeasured in production terms.** See §7.
- **Writing quality is unmeasured.** See §7.
- Corpus is US-only, English-only, five documents. `jurisdiction` and `locale`
  are in the schema and the filters so a second one can be added without a
  migration, but no second one exists.
- No re-ranking beyond RRF. `use_semantic_ranker` is wired but off; Azure's
  semantic ranker is a paid tier feature.
