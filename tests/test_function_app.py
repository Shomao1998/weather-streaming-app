"""Registration tests.

The previous version of this project deployed successfully and then did
nothing, because a v2 decorator-based app was packaged in a v1 folder layout
and the host discovered zero functions. Nothing in a unit test suite would have
caught that — but importing the app and asking it what it registered would
have. That is what this file does.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FUNCTIONS_ROOT = REPO_ROOT / "src" / "functions"

EXPECTED = {
    "ingest_current": "timerTrigger",
    "ingest_forecast": "timerTrigger",
    "archive_to_bronze": "eventHubTrigger",
    "curate": "timerTrigger",
    "api_latest": "httpTrigger",
    "api_timeseries": "httpTrigger",
    "api_breaches": "httpTrigger",
    "api_advice": "httpTrigger",
    "api_advice_feedback": "httpTrigger",
    "api_ask": "httpTrigger",
    "health": "httpTrigger",
}


@pytest.fixture(scope="module")
def registered() -> dict[str, list[str]]:
    import function_app

    return {
        fn.get_function_name(): [b.type for b in fn.get_bindings()]
        for fn in function_app.app.get_functions()
    }


def test_every_expected_function_is_registered(registered):
    assert set(registered) == set(EXPECTED)


@pytest.mark.parametrize(("name", "trigger"), sorted(EXPECTED.items()))
def test_function_has_the_right_trigger(registered, name, trigger):
    assert trigger in registered[name]


def test_deployment_package_has_host_json_at_its_root():
    # host.json one directory below the package root is exactly what broke the
    # previous deployment.
    assert (FUNCTIONS_ROOT / "host.json").is_file()
    assert (FUNCTIONS_ROOT / "function_app.py").is_file()
    assert (FUNCTIONS_ROOT / "requirements.txt").is_file()


def test_no_v1_function_json_files_remain():
    stray = list(FUNCTIONS_ROOT.rglob("function.json"))
    assert stray == [], f"v1 artefacts would shadow the v2 model: {stray}"


def test_local_settings_are_not_committed():
    assert not (FUNCTIONS_ROOT / "local.settings.json").exists()
    assert (FUNCTIONS_ROOT / "local.settings.json.example").is_file()
