"""Azure Functions entry point (Python v2 programming model).

This file only wires triggers to work defined in the ``weather`` package. It
must sit next to ``host.json`` at the root of the deployment package, and there
must be no ``function.json`` files anywhere — mixing the v1 and v2 models is
what stopped the previous version from registering any function at all.

Two rules apply to this file and nothing else in the project, because the
Python worker introspects it to discover functions:

* **No ``from __future__ import annotations``.** PEP 563 turns annotations into
  strings, and the worker reads them to decide binding types.
* **``typing.List[...]``, not ``list[...]``.** The worker does not recognise
  PEP 585 builtin generics when resolving a ``cardinality=MANY`` binding. It
  does not raise a useful error either: indexing fails silently and the host
  reports *zero* functions — including every unrelated one in this file.
"""

import hashlib
import json
import logging
from typing import List

import azure.functions as func

from weather import pipeline, serving
from weather.advice import AdviceService, InvalidLocation, new_session_id
from weather.advice import factory as advice_factory
from weather.config import ConfigError, get_settings

app = func.FunctionApp()
logger = logging.getLogger("weather")


def _json_response(payload: dict, status_code: int = 200) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(payload, ensure_ascii=False, default=str),
        status_code=status_code,
        mimetype="application/json",
        # The dashboard polls these; a short cache absorbs refresh loops
        # without making the page feel stale.
        headers={"Cache-Control": "public, max-age=30"},
    )


def _serve(blob_name: str) -> func.HttpResponse:
    try:
        return _json_response(serving.read_serving_document(blob_name))
    except serving.ServingDataUnavailable as exc:
        return _json_response({"status": "unavailable", "detail": str(exc)}, 503)


@app.function_name(name="ingest_current")
@app.timer_trigger(
    schedule="%INGEST_CURRENT_SCHEDULE%",
    arg_name="timer",
    run_on_startup=False,
    # Schedule monitoring persists a status blob on every tick, which cannot
    # keep up with a sub-minute schedule — with it enabled this function never
    # fires at all. The slower timers below keep it.
    use_monitor=False,
)
def ingest_current(timer: func.TimerRequest) -> None:
    """Fast path: current conditions and air quality, every 30 seconds."""
    if timer.past_due:
        logger.warning("ingest_current timer is past due.")
    try:
        result = pipeline.ingest_current()
    except ConfigError:
        logger.exception("Configuration error; fix App Settings before this can run.")
        raise
    if not result.succeeded:
        # Raising marks the invocation as failed so the Azure Monitor rule on
        # failure count can fire, rather than the run silently degrading.
        raise RuntimeError(f"Ingest failed for locations: {result.failed_locations}")


@app.function_name(name="ingest_forecast")
@app.timer_trigger(
    schedule="%INGEST_FORECAST_SCHEDULE%",
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
def ingest_forecast(timer: func.TimerRequest) -> None:
    """Slow path: daily forecast plus active alerts, every 30 minutes."""
    if timer.past_due:
        logger.warning("ingest_forecast timer is past due.")
    result = pipeline.ingest_forecast()
    if not result.succeeded:
        raise RuntimeError(f"Forecast ingest failed for: {result.failed_locations}")


@app.function_name(name="archive_to_bronze")
@app.event_hub_message_trigger(
    arg_name="events",
    event_hub_name="%EVENT_HUB_NAME%",
    connection="EVENT_HUB_CONNECTION",
    consumer_group="%EVENT_HUB_CONSUMER_GROUP%",
    cardinality=func.Cardinality.MANY,
)
def archive_to_bronze(events: List[func.EventHubEvent]) -> None:  # noqa: UP006
    """Drain the stream into the lake. Replaces (paid) Event Hubs Capture."""
    payloads = []
    for event in events:
        try:
            payloads.append(json.loads(event.get_body().decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.exception("Dropping an unparseable Event Hub message.")
    if payloads:
        pipeline.archive_events(payloads)


@app.function_name(name="curate")
@app.timer_trigger(
    schedule="%CURATE_SCHEDULE%",
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
def curate(timer: func.TimerRequest) -> None:
    """bronze -> silver (Power BI) and serving (public dashboard), hourly."""
    pipeline.curate(hours=24)


@app.function_name(name="api_latest")
@app.route(route="api/latest", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def api_latest(req: func.HttpRequest) -> func.HttpResponse:
    """Most recent reading per location."""
    return _serve(pipeline.SERVING_LATEST_BLOB)


@app.function_name(name="api_timeseries")
@app.route(route="api/timeseries", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def api_timeseries(req: func.HttpRequest) -> func.HttpResponse:
    """24 hours of de-duplicated observations per location."""
    return _serve(pipeline.SERVING_TIMESERIES_BLOB)


@app.function_name(name="api_breaches")
@app.route(route="api/breaches", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def api_breaches(req: func.HttpRequest) -> func.HttpResponse:
    """Recent threshold breaches, newest first."""
    return _serve(pipeline.SERVING_BREACHES_BLOB)


SESSION_HEADER = "X-Advice-Session"

# A question is a retrieval hint, not an input channel. Bounding it keeps
# prompt size predictable and leaves no room for pasted instructions.
MAX_QUESTION_CHARS = 200


def _session_id(req: func.HttpRequest) -> str:
    """An anonymous id the client keeps for its browser session.

    Generated server-side when absent so a first-time caller still gets
    consistent frequency control; nothing else about the caller is recorded.
    """
    return (
        req.headers.get(SESSION_HEADER)
        or req.params.get("session")
        or new_session_id()
    )


@app.function_name(name="api_advice")
@app.route(route="api/advice", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def api_advice(req: func.HttpRequest) -> func.HttpResponse:
    """At most one advice card for a location, or 204 when there is nothing to say.

    Every failure mode here degrades to "no card". The dashboard asks for
    advice *after* it has already rendered the weather, and nothing in this
    handler is allowed to make that page worse.

    `q` is an optional free-text question. It only ever influences wording and
    which passages are retrieved — never whether a card appears, which trigger
    fires, or how severe it is. Those stay with the rule engine, so a question
    cannot talk the system into downplaying a hazard.
    """
    location = (req.params.get("location") or "").strip()
    if not location:
        return _json_response({"error": "location is required"}, 400)

    question = (req.params.get("q") or "").strip()
    if len(question) > MAX_QUESTION_CHARS:
        return _json_response(
            {"error": f"q must be at most {MAX_QUESTION_CHARS} characters"}, 400
        )

    session_id = _session_id(req)
    try:
        snapshot = serving.read_serving_document(pipeline.SERVING_LATEST_BLOB)
    except serving.ServingDataUnavailable:
        return func.HttpResponse(status_code=204, headers={SESSION_HEADER: session_id})
    except Exception:
        logger.exception("Advice: could not read the weather snapshot.")
        return func.HttpResponse(status_code=204, headers={SESSION_HEADER: session_id})

    try:
        result = AdviceService(provider=advice_factory.get_provider()).build(
            snapshot, location, session_id, question=question or None
        )
    except InvalidLocation:
        return _json_response(
            {"error": f"unknown location '{location}'"}, 400
        )
    except Exception:
        logger.exception("Advice: evaluation failed.")
        return func.HttpResponse(status_code=204, headers={SESSION_HEADER: session_id})

    if not result.has_card:
        return func.HttpResponse(
            status_code=204,
            headers={SESSION_HEADER: session_id, "X-Advice-Outcome": str(result.outcome)},
        )

    card = result.card.to_dict()
    response = _json_response(card)
    response.headers[SESSION_HEADER] = session_id
    # Cacheable, but never past the card's own expiry, and never across a new
    # snapshot: the recommendation id changes when the observation does. The
    # question is folded in because two questions against the same snapshot
    # produce the same recommendation id but different copy.
    response.headers["Cache-Control"] = "private, max-age=60"
    etag_seed = card["recommendation_id"]
    if question:
        etag_seed += ":" + hashlib.sha256(question.encode("utf-8")).hexdigest()[:12]
    response.headers["ETag"] = f'"{etag_seed}"'
    return response


@app.function_name(name="api_advice_feedback")
@app.route(
    route="api/advice/feedback", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS
)
def api_advice_feedback(req: func.HttpRequest) -> func.HttpResponse:
    """Record one interaction with a card. Mutes are the only event that
    changes future behaviour; the rest exist to measure whether this feature
    is worth keeping."""
    try:
        payload = req.get_json()
    except ValueError:
        return _json_response({"error": "body must be JSON"}, 400)
    if not isinstance(payload, dict):
        return _json_response({"error": "body must be a JSON object"}, 400)

    payload.setdefault("session_id", _session_id(req))
    try:
        AdviceService().record_feedback(payload)
    except ValueError as exc:
        return _json_response({"error": str(exc)}, 400)
    except Exception:
        logger.exception("Advice: feedback could not be recorded.")
        return func.HttpResponse(status_code=202)

    return func.HttpResponse(status_code=202)


@app.function_name(name="health")
@app.route(route="health", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def health(req: func.HttpRequest) -> func.HttpResponse:
    """Liveness probe that also proves configuration resolved correctly."""
    try:
        settings = get_settings()
    except ConfigError as exc:
        return func.HttpResponse(
            json.dumps({"status": "misconfigured", "detail": str(exc)}),
            status_code=503,
            mimetype="application/json",
        )
    body = {
        "status": "ok",
        "environment": settings.environment,
        "locations": list(settings.weather.locations),
        "event_hub_enabled": settings.event_hub.enabled,
        "storage_enabled": settings.storage.enabled,
    }
    return func.HttpResponse(json.dumps(body), mimetype="application/json")
