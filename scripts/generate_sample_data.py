"""Generate the sample serving documents the dashboard falls back to.

Run from the repository root:

    python scripts/generate_sample_data.py

The samples are produced by the real transform and serving code, so the shapes
in dashboard/data/ can never drift from what the deployed pipeline emits — if
this script needs changing, the dashboard needed changing too.
"""

from __future__ import annotations

import json
import math
import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src" / "functions"))

from weather import monitoring, pipeline, transform  # noqa: E402
from weather.advice import AdviceService  # noqa: E402
from weather.advice.repository import InMemoryAdviceRepository  # noqa: E402
from weather.config import MonitoringSettings, Settings  # noqa: E402
from weather.models import (  # noqa: E402
    ForecastDayRecord,
    ForecastHourRecord,
    Location,
    make_record_id,
)

OUTPUT_DIR = REPO_ROOT / "dashboard" / "data"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "current_tokyo.json"

CITIES = [
    {"name": "Tokyo", "region": "Tokyo", "lat": 35.6895, "lon": 139.6917, "base_temp": 31.0},
    {"name": "Osaka", "region": "Osaka", "lat": 34.6937, "lon": 135.5023, "base_temp": 33.5},
    {"name": "Sapporo", "region": "Hokkaido", "lat": 43.0618, "lon": 141.3545, "base_temp": 24.0},
]

# The upstream API refreshes roughly every 15 minutes.
OBSERVATION_INTERVAL_MINUTES = 15
HOURS = 24


def build_hourly(now: datetime) -> list[dict]:
    """An hourly forecast for the look-ahead window, one entry per city.

    The advice engine's rain rule reads this; without it the sample dashboard
    would show a card the deployed one never could.
    """
    random.seed(4711)
    rows: list[dict] = []
    for index, city in enumerate(CITIES):
        location = Location(name=city["name"], region=city["region"],
                            lat=city["lat"], lon=city["lon"])
        for hour in range(6):
            moment = (now + timedelta(hours=hour)).replace(minute=0, second=0, microsecond=0)
            # Hour 0 is the current hour, which the serving layer skips as
            # already past — so the visible card has to come from hour 1.
            chance = 85 if (index == 0 and hour == 1) else random.randint(0, 45)
            stamp = moment.isoformat().replace("+00:00", "Z")
            rows.append(
                ForecastHourRecord(
                    record_id=make_record_id("forecast_hour", location.key, stamp),
                    location_key=location.key,
                    location=location,
                    time_utc=stamp,
                    ingested_at_utc=now.isoformat().replace("+00:00", "Z"),
                    temp_c=round(city["base_temp"] + random.uniform(-2, 2), 1),
                    precip_mm=round(chance / 100 * 3, 1),
                    chance_of_rain=chance,
                    wind_kph=round(random.uniform(5, 25), 1),
                    condition_text="Patchy rain nearby" if chance >= 50 else "Sunny",
                ).to_dict()
            )
    return rows


def build_daily(now: datetime) -> list[dict]:
    """A three-day daily forecast per city, so the offline assistant can answer
    "会不会下雨 / 明天几度" from the same data the deployed one uses."""
    random.seed(8127)
    rows: list[dict] = []
    for city in CITIES:
        location = Location(name=city["name"], region=city["region"],
                            lat=city["lat"], lon=city["lon"])
        for offset in range(3):
            day = (now + timedelta(days=offset)).date().isoformat()
            # Give Osaka a wet "tomorrow" so the demo question ("我在大阪，明天
            # 会下雨吗") has a clear, obviously-correct answer.
            chance = 70 if (city["name"] == "Osaka" and offset == 1) else random.randint(0, 40)
            hi = round(city["base_temp"] + random.uniform(1, 4))
            rows.append(
                ForecastDayRecord(
                    record_id=make_record_id("forecast", location.key, day),
                    location_key=location.key,
                    location=location,
                    date=day,
                    ingested_at_utc=now.isoformat().replace("+00:00", "Z"),
                    maxtemp_c=hi,
                    mintemp_c=hi - random.randint(5, 8),
                    avgtemp_c=hi - 3,
                    maxwind_kph=round(random.uniform(10, 30), 1),
                    totalprecip_mm=round(chance / 100 * 6, 1),
                    avghumidity=random.randint(50, 85),
                    daily_chance_of_rain=chance,
                    uv=random.randint(3, 8),
                    condition_text="Light rain" if chance >= 50 else "Partly cloudy",
                ).to_dict()
            )
    return rows


def build_rows() -> tuple[list[dict], list[dict]]:
    random.seed(20260801)
    template = json.loads(FIXTURE.read_text(encoding="utf-8"))
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    thresholds = MonitoringSettings()

    current_rows: list[dict] = []
    breach_rows: list[dict] = []
    steps = (HOURS * 60) // OBSERVATION_INTERVAL_MINUTES

    for city in CITIES:
        for step in range(steps + 1):
            moment = now - timedelta(minutes=OBSERVATION_INTERVAL_MINUTES * (steps - step))
            # A daily temperature curve plus a little noise reads as real data
            # on the chart, which flat random values do not.
            hour_angle = (moment.hour + moment.minute / 60) / 24 * 2 * math.pi
            temp = city["base_temp"] - 4.5 * math.cos(hour_angle - 1.2) + random.uniform(-0.5, 0.5)

            payload = json.loads(json.dumps(template))
            payload["location"].update(
                {
                    "name": city["name"],
                    "region": city["region"],
                    "lat": city["lat"],
                    "lon": city["lon"],
                }
            )
            payload["current"].update(
                {
                    "last_updated_epoch": int(moment.timestamp()),
                    "temp_c": round(temp, 1),
                    "feelslike_c": round(temp + random.uniform(1.5, 5.0), 1),
                    "humidity": int(max(35, min(95, 68 + random.uniform(-12, 12)))),
                    "wind_kph": round(max(2.0, random.gauss(14, 6)), 1),
                    "cloud": random.randint(0, 90),
                    "uv": round(max(0.0, 8 * math.sin(hour_angle - 1.2)), 1),
                    "pressure_mb": round(random.gauss(1010, 4), 1),
                }
            )
            payload["current"]["air_quality"]["pm2_5"] = round(
                max(3.0, random.gauss(20, 9)), 1
            )

            record = transform.to_current_record(payload, ingested_at=moment)
            current_rows.append(record.to_dict())
            for breach in monitoring.evaluate(record, thresholds):
                row = breach.to_dict()
                # evaluate() stamps detection with the wall clock, which is
                # right in production (detection happens at ingest) but makes
                # every sample breach look like it fired seconds ago.
                row["detected_at_utc"] = moment.isoformat().replace("+00:00", "Z")
                breach_rows.append(row)

    return current_rows, breach_rows


def sample_provider():
    """The RAG provider over the committed index, with a scripted model."""
    index_path = REPO_ROOT / "knowledge" / "processed" / "index.json"
    if not index_path.exists():
        return None  # falls back to the template provider

    from weather.advice.embeddings import HashingEmbedder
    from weather.advice.knowledge import KnowledgeIndex
    from weather.advice.llm import ScriptedChatClient
    from weather.advice.models import AdviceTrigger
    from weather.advice.rag import RagAdviceProvider
    from weather.advice.retrieval import LocalIndexRetriever
    from weather.config import RagSettings

    index = KnowledgeIndex.load(index_path)
    retriever = LocalIndexRetriever(index, HashingEmbedder())
    settings = Settings(rag=RagSettings(enabled=True))

    rain = next(c for c in index.chunks if "rain" in c.hazard_types)
    response = json.dumps(
        {
            "title": "一小时内可能下雨",
            "message": "出门记得带伞，路上多留些时间。",
            "advice_codes": ["CARRY_UMBRELLA", "ALLOW_EXTRA_TRAVEL_TIME"],
            "supporting_chunk_ids": [rain.chunk_id],
        },
        ensure_ascii=False,
    )
    del AdviceTrigger  # imported only to fail loudly if the enum moves
    return RagAdviceProvider(
        retriever=retriever,
        chat_client=ScriptedChatClient([response] * 8),
        settings=settings,
    )


def main() -> int:
    current_rows, breach_rows = build_rows()
    hourly_rows = build_hourly(datetime.now(UTC))
    daily_rows = build_daily(datetime.now(UTC))
    payloads = pipeline.build_serving_payloads(
        current_rows, breach_rows, hourly_rows, daily_rows
    )

    # Run the real advice service over the sample snapshot, so the offline
    # dashboard shows exactly the card the deployed one would.
    #
    # The provider is the retrieval-grounded one, wired to the committed
    # knowledge index and a scripted model. That makes the sample card carry
    # real chunk ids and real source URLs, so the offline dashboard exercises
    # the citation row instead of only ever showing the template path. No
    # network call and no spend: the "model" here composes its answer from the
    # retrieved passages, and it is labelled as such.
    service = AdviceService(
        settings=Settings(),
        provider=sample_provider(),
        repository=InMemoryAdviceRepository(),
    )
    advice = {"card": None}
    for entry in payloads[pipeline.SERVING_LATEST_BLOB]["locations"]:
        result = service.build(
            payloads[pipeline.SERVING_LATEST_BLOB],
            entry["name"],
            "sample-session",
        )
        if result.has_card:
            advice = {"card": result.card.to_dict()}
            break
    payloads["advice.json"] = advice

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, payload in payloads.items():
        path = OUTPUT_DIR / name
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {path.relative_to(REPO_ROOT)} ({path.stat().st_size:,} bytes)")

    print(
        f"\n{len(current_rows)} observations across {len(CITIES)} cities, "
        f"{len(breach_rows)} threshold breaches, {len(hourly_rows)} forecast hours"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
