"""The eval suite runs in CI as a gate, not as a report nobody reads.

Running the runners here means a corpus edit that degrades retrieval, or a
prompt change that lets an unresolvable citation through, fails the build
rather than being discovered in production.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
EVALS = REPO_ROOT / "evals"
sys.path.insert(0, str(EVALS))

from metrics import load_cases  # noqa: E402


def test_the_case_set_is_large_enough_to_mean_something():
    cases = load_cases()
    assert len(cases) >= 30
    kinds = {c["kind"] for c in cases}
    assert kinds == {"retrieval", "safety", "abstain"}


def test_every_case_is_well_formed():
    from weather.advice.models import AdviceTrigger

    seen = set()
    for case in load_cases():
        assert case["id"] not in seen, f"duplicate id {case['id']}"
        seen.add(case["id"])
        AdviceTrigger(case["trigger"])  # raises on an unknown trigger
        assert case["expect"], case["id"]
        assert case["weather"], case["id"]


def test_every_expected_document_exists_in_the_corpus(knowledge_index):
    """A case that names a document we do not ship would pass vacuously."""
    known = {c.source_document_id for c in knowledge_index.chunks}
    for case in load_cases():
        for document in case["expect"].get("documents", []):
            assert document in known, f"{case['id']} expects unknown document {document}"


def _run(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(EVALS / script), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=180,
    )


@pytest.mark.slow
def test_the_retrieval_eval_passes_its_gate():
    result = _run("run_retrieval_eval.py", "--fail-under", "0.95")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "hazard_leaks" in result.stdout


@pytest.mark.slow
def test_the_generation_eval_passes_its_gate():
    """Zero unresolvable citations, zero banned output, zero missed fallbacks."""
    result = _run("run_generation_eval.py")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "measures_writing_quality" in result.stdout
