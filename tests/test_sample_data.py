"""The committed dashboard samples must match what the pipeline actually emits.

The dashboard renders these files when no API is configured, which makes them
the thing reviewers see first. If the serving payload gains or loses a field
and the samples are not regenerated, the local view silently stops matching
production — so the shape is asserted here rather than trusted.

Byte equality is not checkable: the generator anchors timestamps to "now" so
the sample chart always shows a full recent day.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from weather import pipeline, transform

SAMPLE_DIR = Path(__file__).resolve().parents[1] / "dashboard" / "data"


@pytest.fixture(scope="module")
def reference_payloads(request) -> dict:
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "current_tokyo.json").read_text(encoding="utf-8")
    )
    record = transform.to_current_record(fixture)
    return pipeline.build_serving_payloads([record.to_dict()], [])


@pytest.mark.parametrize(
    "name",
    [
        pipeline.SERVING_LATEST_BLOB,
        pipeline.SERVING_TIMESERIES_BLOB,
        pipeline.SERVING_BREACHES_BLOB,
    ],
)
def test_sample_file_exists(name):
    assert (SAMPLE_DIR / name).is_file(), f"run scripts/generate_sample_data.py to create {name}"


def _load(name: str) -> dict:
    return json.loads((SAMPLE_DIR / name).read_text(encoding="utf-8"))


def test_latest_sample_has_the_production_shape(reference_payloads):
    sample = _load(pipeline.SERVING_LATEST_BLOB)
    reference = reference_payloads[pipeline.SERVING_LATEST_BLOB]

    assert set(sample) == set(reference)
    assert set(sample["locations"][0]) == set(reference["locations"][0])


def test_timeseries_sample_has_the_production_shape(reference_payloads):
    sample = _load(pipeline.SERVING_TIMESERIES_BLOB)
    reference = reference_payloads[pipeline.SERVING_TIMESERIES_BLOB]

    assert set(sample) == set(reference)
    assert set(sample["series"][0]) == set(reference["series"][0])
    assert set(sample["series"][0]["points"][0]) == set(reference["series"][0]["points"][0])


def test_breaches_sample_has_the_production_shape():
    sample = _load(pipeline.SERVING_BREACHES_BLOB)

    assert set(sample) == {"generated_at_utc", "breaches"}
    assert sample["breaches"], "the sample should include breaches so the feed is not empty"
    assert {"severity", "message", "detected_at_utc", "metric"} <= set(sample["breaches"][0])


def test_samples_cover_several_locations_and_a_full_day():
    timeseries = _load(pipeline.SERVING_TIMESERIES_BLOB)

    assert len(timeseries["series"]) >= 2, "a single-series chart does not exercise the legend"
    assert len(timeseries["series"][0]["points"]) >= 24
