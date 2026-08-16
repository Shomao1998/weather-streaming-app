"""Metric definitions shared by both eval runners.

Every metric here is computed by comparing values — chunk ids, document ids,
booleans. None of them asks a model to judge anything. A model that grades its
own family's output can be wrong in exactly the direction that matters, so the
numbers this project reports are the ones a person can recompute by hand.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CASES = Path(__file__).resolve().parent / "rag_cases.jsonl"


def load_cases(path: Path = CASES, kind: str | None = None) -> list[dict[str, Any]]:
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return [r for r in rows if kind is None or r["kind"] == kind]


# -- retrieval --------------------------------------------------------------


def hit_at_k(retrieved_documents: list[str], expected: list[str], k: int) -> bool:
    """Whether any expected document appears in the first k results."""
    return bool(set(retrieved_documents[:k]) & set(expected))


def precision_at_k(retrieved_documents: list[str], expected: list[str], k: int) -> float:
    if not retrieved_documents[:k]:
        return 0.0
    return sum(1 for d in retrieved_documents[:k] if d in expected) / len(retrieved_documents[:k])


def reciprocal_rank(retrieved_documents: list[str], expected: list[str]) -> float:
    for index, document in enumerate(retrieved_documents, start=1):
        if document in expected:
            return 1.0 / index
    return 0.0


def heading_hit(headings: list[str], wanted: list[str]) -> bool:
    """Whether the retrieved passages include one of the expected sections.

    Stricter than document-level hit rate, and the one that actually predicts
    whether the generated card will say something useful: retrieving the right
    document but the wrong section produces advice that is on-topic and
    unhelpful.
    """
    return any(w.lower() in h.lower() for h in headings for w in wanted)


@dataclass
class RetrievalReport:
    total: int = 0
    hit_1: int = 0
    hit_3: int = 0
    heading_cases: int = 0
    heading_hits: int = 0
    leak_cases: int = 0
    leaks: int = 0
    empty: int = 0
    # Split by question language. The offline embedder is lexical and the
    # corpus is English, so a Chinese question is carried almost entirely by
    # the trigger's English seed text. Reporting the two separately keeps an
    # offline number from being read as a production one.
    heading_cases_en: int = 0
    heading_hits_en: int = 0
    heading_cases_zh: int = 0
    heading_hits_zh: int = 0
    precision_sum: float = 0.0
    rr_sum: float = 0.0
    failures: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        total = max(self.total, 1)
        return {
            "cases": self.total,
            "hit_at_1": round(self.hit_1 / total, 3),
            "hit_at_3": round(self.hit_3 / total, 3),
            "precision_at_k": round(self.precision_sum / total, 3),
            "mrr": round(self.rr_sum / total, 3),
            "heading_hit_rate": (
                round(self.heading_hits / self.heading_cases, 3) if self.heading_cases else None
            ),
            "heading_hit_rate_en_question": (
                round(self.heading_hits_en / self.heading_cases_en, 3)
                if self.heading_cases_en
                else None
            ),
            "heading_hit_rate_zh_question": (
                round(self.heading_hits_zh / self.heading_cases_zh, 3)
                if self.heading_cases_zh
                else None
            ),
            "hazard_leaks": self.leaks,
            "empty_results": self.empty,
        }


def is_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text or "")


# -- generation -------------------------------------------------------------


@dataclass
class GenerationReport:
    total: int = 0
    grounded: int = 0
    fell_back: int = 0
    schema_failures: int = 0
    citation_failures: int = 0
    expected_fallbacks: int = 0
    expected_fallbacks_met: int = 0
    unresolvable_citations: int = 0
    banned_output: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    failures: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        total = max(self.total, 1)
        return {
            "cases": self.total,
            "grounded_rate": round(self.grounded / total, 3),
            "fallback_rate": round(self.fell_back / total, 3),
            # The three that must be zero for the feature to ship.
            "unresolvable_citations": self.unresolvable_citations,
            "banned_output_reaching_a_card": self.banned_output,
            "expected_fallbacks_missed": self.expected_fallbacks - self.expected_fallbacks_met,
            "schema_failures": self.schema_failures,
            "citation_failures": self.citation_failures,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "estimated_cost_usd": round(self.cost_usd, 6),
        }


def print_report(title: str, payload: dict[str, Any], failures: list[dict[str, Any]]) -> None:
    print(f"\n{title}")
    print("=" * len(title))
    width = max(len(k) for k in payload)
    for key, value in payload.items():
        print(f"  {key:<{width}}  {value}")
    if failures:
        print(f"\n  {len(failures)} case(s) below expectation:")
        for failure in failures[:12]:
            print(f"    {failure['id']:<14} {failure['reason']}")
        if len(failures) > 12:
            print(f"    ... and {len(failures) - 12} more")
