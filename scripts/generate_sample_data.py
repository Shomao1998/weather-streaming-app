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
from weather.config import MonitoringSettings  # noqa: E402

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


def main() -> int:
    current_rows, breach_rows = build_rows()
    payloads = pipeline.build_serving_payloads(current_rows, breach_rows)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, payload in payloads.items():
        path = OUTPUT_DIR / name
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {path.relative_to(REPO_ROOT)} ({path.stat().st_size:,} bytes)")

    print(
        f"\n{len(current_rows)} observations across {len(CITIES)} cities, "
        f"{len(breach_rows)} threshold breaches"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
