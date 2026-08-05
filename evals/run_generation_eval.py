"""Score the generation path against `rag_cases.jsonl`.

    python evals/run_generation_eval.py               # scripted client, no cost
    python evals/run_generation_eval.py --azure       # real model, real spend

**What the default run does and does not measure.** Without `--azure` there is
no language model in the loop: a scripted client stands in, and what is scored
is the machinery around generation — schema parsing, citation resolution,
banned-pattern rejection, and whether every failure path falls back. Those are
the properties that decide whether a wrong answer can reach a user, and they
are worth gating on. The default run says nothing about writing quality. The
report labels which client produced it so the two are never confused.

The `abstain` cases are run against deliberately broken dependencies — a dead
retriever, a timing-out model, malformed output, a fabricated citation — because
"it falls back" is a claim that has to be executed, not asserted.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src" / "functions"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from metrics import GenerationReport, load_cases, print_report  # noqa: E402
from run_retrieval_eval import weather_from  # noqa: E402

from weather.advice import grounding  # noqa: E402
from weather.advice import llm as llm_module  # noqa: E402
from weather.advice.embeddings import HashingEmbedder  # noqa: E402
from weather.advice.knowledge import KnowledgeIndex  # noqa: E402
from weather.advice.models import AdviceTrigger  # noqa: E402
from weather.advice.providers import TemplateAdviceProvider  # noqa: E402
from weather.advice.rag import RagAdviceProvider  # noqa: E402
from weather.advice.retrieval import LocalIndexRetriever, RetrievalError  # noqa: E402
from weather.config import RagSettings, Settings  # noqa: E402

INDEX = REPO / "knowledge" / "processed" / "index.json"


class DeadRetriever:
    name, index_version = "dead", "eval"

    def retrieve(self, query):
        raise RetrievalError("simulated retrieval outage")


class EmptyRetriever:
    name, index_version = "empty", "eval"

    def retrieve(self, query):
        return []


class TimingOutClient:
    name, model = "timeout", "eval"

    def complete(self, system, user):
        raise llm_module.LlmTimeout("simulated model timeout")


class MalformedClient:
    name, model = "malformed", "eval"

    def complete(self, system, user):
        return llm_module.LlmResponse(text="I'm sorry, I can't help with that.", model=self.model)


class FabricatingClient:
    """Well-formed output citing a chunk that was never retrieved."""

    name, model = "fabricating", "eval"

    def complete(self, system, user):
        return llm_module.LlmResponse(
            text=json.dumps(
                {
                    "title": "天气提醒",
                    "message": "请注意安全。",
                    "advice_codes": ["HYDRATE"],
                    "supporting_chunk_ids": ["nws-heat-during-999-notreal00000"],
                },
                ensure_ascii=False,
            ),
            model=self.model,
        )


# Each abstain case is bound to the specific outage it claims to survive.
BROKEN = {
    "abstain-001": (EmptyRetriever(), None),
    "abstain-002": (DeadRetriever(), None),
    "abstain-003": (None, TimingOutClient()),
    "abstain-004": (None, MalformedClient()),
    "abstain-005": (None, FabricatingClient()),
}


def grounded_client(retriever, provider, case):
    """A client that cites what this query actually retrieves.

    Stands in for a well-behaved model so the scored path is the real one:
    retrieve, build the prompt, parse, validate, cite.
    """
    chunks = retriever.retrieve(
        provider.build_query(
            AdviceTrigger(case["trigger"]), weather_from(case), case.get("question")
        )
    )
    if not chunks:
        return MalformedClient()
    return llm_module.ScriptedChatClient(
        [
            json.dumps(
                {
                    "title": "天气提醒",
                    "message": "请按官方建议采取防护措施。",
                    "advice_codes": ["HYDRATE"],
                    "supporting_chunk_ids": [chunks[0].chunk_id],
                },
                ensure_ascii=False,
            )
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", default=str(INDEX))
    parser.add_argument("--json", dest="json_out")
    parser.add_argument(
        "--azure",
        action="store_true",
        help="Use the configured Azure OpenAI deployment. Costs money.",
    )
    args = parser.parse_args()

    index = KnowledgeIndex.load(args.index)
    retriever = LocalIndexRetriever(index, HashingEmbedder())
    settings = Settings(rag=RagSettings(enabled=True))

    azure_client = None
    if args.azure:
        azure_client = llm_module.get_chat_client(settings)
        if azure_client is None:
            print("--azure was requested but RAG_OPENAI_ENDPOINT / RAG_CHAT_DEPLOYMENT are unset.")
            return 2

    report = GenerationReport()
    client_names: set[str] = set()

    for case in load_cases():
        if case["kind"] == "retrieval" and case.get("question") is None:
            continue

        broken_retriever, broken_client = BROKEN.get(case["id"], (None, None))
        active_retriever = broken_retriever or retriever
        probe = RagAdviceProvider(retriever=retriever, chat_client=None, settings=settings)
        client = (
            broken_client
            or azure_client
            or grounded_client(retriever, probe, case)
        )
        client_names.add(getattr(client, "name", "unknown"))

        provider = RagAdviceProvider(
            retriever=active_retriever, chat_client=client, settings=settings
        )
        content = provider.generate(
            AdviceTrigger(case["trigger"]), weather_from(case), case.get("question")
        )
        telemetry = provider.last_telemetry
        expect = case["expect"]
        report.total += 1

        fell_back = content.generation_method == TemplateAdviceProvider.name
        if fell_back:
            report.fell_back += 1
            reason = telemetry.fallback_reason
            if "structured JSON" in reason:
                report.schema_failures += 1
            if "validation failed" in reason and "citation" in reason:
                report.citation_failures += 1
        else:
            report.grounded += 1
            report.prompt_tokens += telemetry.prompt_tokens
            report.completion_tokens += telemetry.completion_tokens
            report.cost_usd += telemetry.estimated_cost_usd

        # A card must always exist, grounded or not. This is the guarantee the
        # whole design rests on.
        if not (content.title and content.message):
            report.failures.append({"id": case["id"], "reason": "produced no card at all"})

        if expect.get("expect_fallback"):
            report.expected_fallbacks += 1
            if fell_back:
                report.expected_fallbacks_met += 1
            else:
                report.failures.append(
                    {"id": case["id"], "reason": "should have fallen back but did not"}
                )

        # Every citation on a shipped card must resolve to a live chunk.
        for source in content.sources:
            chunk = index.by_id(source.chunk_id)
            if chunk is None or not chunk.is_effective():
                report.unresolvable_citations += 1
                report.failures.append(
                    {"id": case["id"], "reason": f"citation {source.chunk_id} does not resolve"}
                )

        for pattern in grounding.BANNED_PATTERNS:
            if pattern.search(content.title) or pattern.search(content.message):
                report.banned_output += 1
                report.failures.append(
                    {"id": case["id"], "reason": "banned pattern reached a card"}
                )
                break

        for code in content.advice_codes:
            if code not in grounding.ADVICE_CODES:
                report.failures.append(
                    {"id": case["id"], "reason": f"advice code {code} is outside the vocabulary"}
                )

    payload = report.as_dict()
    payload["client"] = "azure-openai" if args.azure else "+".join(sorted(client_names))
    payload["measures_writing_quality"] = bool(args.azure)
    payload["prompt_version"] = llm_module.PROMPT_VERSION
    payload["index_version"] = index.index_version
    print_report("Generation evaluation", payload, report.failures)

    if not args.azure:
        print(
            "\n  Note: no language model was called. This run scores parsing, citation\n"
            "  resolution, guardrails and fallback behaviour — not writing quality.\n"
            "  Use --azure against a real deployment for that."
        )

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # The three conditions under which this feature must not ship.
    blocking = (
        report.unresolvable_citations,
        report.banned_output,
        report.expected_fallbacks - report.expected_fallbacks_met,
    )
    if any(blocking):
        print("\nFAIL: unresolvable citations, banned output, or a missed fallback.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
