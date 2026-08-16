"""Score the retrieval layer against `rag_cases.jsonl`.

    python evals/run_retrieval_eval.py                  # local index, no cost
    python evals/run_retrieval_eval.py --json report.json

Runs with no Azure resources and no model, which is what makes it usable as a
regression gate: re-ingest the corpus, re-run this, and see whether ranking got
better or worse before anything is deployed.

`--fail-under` makes it a gate rather than a report.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src" / "functions"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from metrics import (  # noqa: E402
    RetrievalReport,
    heading_hit,
    hit_at_k,
    is_cjk,
    load_cases,
    precision_at_k,
    print_report,
    reciprocal_rank,
)

from weather.advice.embeddings import HashingEmbedder, get_embedder  # noqa: E402
from weather.advice.knowledge import KnowledgeIndex  # noqa: E402
from weather.advice.models import AdviceTrigger, WeatherContext  # noqa: E402
from weather.advice.rag import RagAdviceProvider  # noqa: E402
from weather.advice.retrieval import LocalIndexRetriever  # noqa: E402
from weather.config import RagSettings, Settings  # noqa: E402

INDEX = REPO / "knowledge" / "processed" / "index.json"


def weather_from(case: dict) -> WeatherContext:
    payload = case["weather"]
    return WeatherContext(
        location="Eval",
        location_key="eval",
        observed_at_utc=None,
        temp_c=payload.get("temp_c"),
        feelslike_c=payload.get("feelslike_c"),
        uv=payload.get("uv"),
        wind_kph=payload.get("wind_kph"),
        precip_chance_next_hour=payload.get("precip_chance_next_hour"),
        condition_text="",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", default=str(INDEX))
    parser.add_argument("--top-k", type=int, default=4)
    parser.add_argument("--json", dest="json_out")
    parser.add_argument(
        "--fail-under",
        type=float,
        default=None,
        help="Exit non-zero if hit_at_3 falls below this.",
    )
    parser.add_argument("--azure-embeddings", action="store_true")
    args = parser.parse_args()

    index = KnowledgeIndex.load(args.index)
    embedder = get_embedder() if args.azure_embeddings else HashingEmbedder()
    retriever = LocalIndexRetriever(index, embedder)
    settings = Settings(rag=RagSettings(enabled=True, top_k=args.top_k))
    provider = RagAdviceProvider(retriever=retriever, chat_client=None, settings=settings)

    report = RetrievalReport()
    for case in load_cases(kind="retrieval"):
        expected = case["expect"].get("documents", [])
        query = provider.build_query(
            AdviceTrigger(case["trigger"]), weather_from(case), case.get("question")
        )
        results = retriever.retrieve(query)
        documents = [r.chunk.source_document_id for r in results]
        headings = [r.chunk.heading for r in results]

        report.total += 1
        if not results:
            report.empty += 1
            report.failures.append({"id": case["id"], "reason": "no results"})
            continue

        if hit_at_k(documents, expected, 1):
            report.hit_1 += 1
        if hit_at_k(documents, expected, 3):
            report.hit_3 += 1
        else:
            report.failures.append(
                {"id": case["id"], "reason": f"expected {expected}, got {documents[:3]}"}
            )
        report.precision_sum += precision_at_k(documents, expected, args.top_k)
        report.rr_sum += reciprocal_rank(documents, expected)

        wanted_headings = case["expect"].get("headings_any")
        if wanted_headings:
            chinese = is_cjk(case.get("question", ""))
            report.heading_cases += 1
            if chinese:
                report.heading_cases_zh += 1
            else:
                report.heading_cases_en += 1
            if heading_hit(headings, wanted_headings):
                report.heading_hits += 1
                if chinese:
                    report.heading_hits_zh += 1
                else:
                    report.heading_hits_en += 1
            else:
                report.failures.append(
                    {
                        "id": case["id"],
                        "reason": f"wanted a section in {wanted_headings}, got {headings}",
                    }
                )

        forbidden = case["expect"].get("must_not_documents")
        if forbidden:
            report.leak_cases += 1
            leaked = set(documents) & set(forbidden)
            if leaked:
                report.leaks += 1
                report.failures.append(
                    {"id": case["id"], "reason": f"hazard filter leaked into {sorted(leaked)}"}
                )

    payload = report.as_dict()
    payload["embedder"] = embedder.name
    payload["index_version"] = index.index_version
    print_report("Retrieval evaluation", payload, report.failures)

    if embedder.name.startswith("hashing"):
        print(
            "\n  Note: the offline embedder matches tokens, not meaning, and the corpus is\n"
            "  English. A Chinese question is therefore carried by the trigger's English seed\n"
            "  text alone. Re-run with --azure-embeddings for a production-representative\n"
            "  cross-lingual number."
        )

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(payload, indent=2), encoding="utf-8")

    if report.leaks:
        print("\nFAIL: a hazard filter leaked. This is never acceptable.")
        return 1
    if args.fail_under is not None and payload["hit_at_3"] < args.fail_under:
        print(f"\nFAIL: hit_at_3 {payload['hit_at_3']} < {args.fail_under}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
