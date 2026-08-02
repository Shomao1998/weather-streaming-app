"""Azure Functions entry point (Python v2 programming model).

This file only wires triggers to work defined in the ``weather`` package. It
must sit next to ``host.json`` at the root of the deployment package, and there
must be no ``function.json`` files anywhere — mixing the v1 and v2 models is
what stopped the previous version from registering any function at all.
"""

from __future__ import annotations

import json
import logging

import azure.functions as func

from weather import pipeline
from weather.config import ConfigError, get_settings

app = func.FunctionApp()
logger = logging.getLogger("weather")


@app.function_name(name="ingest_current")
@app.timer_trigger(
    schedule="%INGEST_CURRENT_SCHEDULE%",
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
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
def archive_to_bronze(events: list[func.EventHubEvent]) -> None:
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
