# Building the report

Written against the schema the pipeline actually emits — 35 columns, verified against a live
`silver/current/date=.../current.parquet` file rather than assumed.

Power BI Desktop is Windows-only. On macOS you need a Windows VM (Parallels, UTM) or a cloud
Windows host. Everything below is Desktop work; nothing here requires a Pro licence.

## 1. Connect

**Get data → Azure → Azure Data Lake Storage Gen2**

```
https://stweatherlakee5lpvy.dfs.core.windows.net/silver
```

Authenticate with **Account key** (see [README.md](README.md) for why, and for the command that
prints it). Choose **Combine → Combine & Transform Data** so Power Query stitches every partition
into one table and keeps the `date=` folder as a column.

Combining the whole tree is safe because the lake holds **exactly one file per day**
(`current/date=YYYY-MM-DD/current.parquet`), overwritten in place on each hourly curate. An
observation therefore appears in exactly one partition — there is no cross-file duplication for
Power Query to fold away, so a plain combine is correct rather than merely convenient.

## 2. Power Query

The silver layer is already de-duplicated (within and across files) and flat, so this is short.

**Set types.** Everything arrives correctly typed except the two timestamps, which the pipeline
writes as ISO-8601 strings:

| Column | Type |
| --- | --- |
| `observed_at_utc`, `ingested_at_utc` | Date/Time/Timezone → then **Convert to UTC** |
| `temp_c`, `feelslike_c`, `wind_kph`, `pressure_mb`, `precip_mm`, `uv`, `aqi_*` (except indexes) | Decimal Number |
| `humidity`, `cloud`, `is_day`, `wind_degree`, `aqi_us_epa_index`, `aqi_gb_defra_index` | Whole Number |
| everything else | Text |

**Drop what the report will never use.** `schema_version`, `source`, `record_type`,
`condition_icon`, `location_tz_id`, `location_localtime`, `aqi_gb_defra_index`. Keep `record_id` —
it is what makes the observation count honest.

**Add a local observation time.** All three cities are `Asia/Tokyo`, so one offset covers them:

```m
= Table.AddColumn(#"Previous Step", "observed_at_local",
    each DateTimeZone.SwitchZone([observed_at_utc], 9), type datetimezone)
```

**Add the ingestion lag in seconds.** This is the column that makes the report a data-engineering
artefact rather than a weather widget:

```m
= Table.AddColumn(#"Previous Step", "ingest_lag_seconds",
    each Duration.TotalSeconds([ingested_at_utc] - [observed_at_utc]), Int64.Type)
```

## 3. Date table

```dax
Date =
ADDCOLUMNS(
    CALENDAR(MIN('current'[observed_at_local]), MAX('current'[observed_at_local])),
    "Year", YEAR([Date]),
    "Month", FORMAT([Date], "YYYY-MM"),
    "Day", DAY([Date]),
    "Weekday", FORMAT([Date], "ddd")
)
```

Mark it as a date table, then relate `Date[Date]` → `current[observed_at_local]` (one-to-many,
single direction).

## 4. Measures

### Volume and freshness — the operational half

```dax
Observations = DISTINCTCOUNT('current'[record_id])
```

`DISTINCTCOUNT` on `record_id`, not `COUNTROWS`. The id is a hash of (location, upstream
observation time), so this counts *distinct observations* even if a partition were ever loaded
twice. Counting rows would silently double on a replay.

```dax
Locations = DISTINCTCOUNT('current'[location_key])

Avg Ingest Lag (s) = AVERAGE('current'[ingest_lag_seconds])

Max Ingest Lag (s) = MAX('current'[ingest_lag_seconds])

Data Age (min) =
DIVIDE(
    DATEDIFF(MAX('current'[observed_at_utc]), UTCNOW(), SECOND),
    60
)
```

`Data Age` is the one to put on the front page. It answers "is this pipeline still alive?" — the
failure mode a dashboard full of green charts hides.

```dax
Collection Efficiency =
-- Distinct observations per poll. The API refreshes every ~15 minutes while the
-- collector runs every 30s, so this sits near 0.03 by design — and a sudden
-- jump to 1.0 would mean de-duplication has stopped working.
VAR Polls = DIVIDE(COUNTROWS('current'), 1)
RETURN DIVIDE([Observations], Polls)
```

### Weather — the analytical half

```dax
Avg Temp = AVERAGE('current'[temp_c])
Max Temp = MAX('current'[temp_c])
Min Temp = MIN('current'[temp_c])

Avg Feels Like = AVERAGE('current'[feelslike_c])

Feels-Like Gap = [Avg Feels Like] - [Avg Temp]

Temp Δ vs Yesterday =
VAR Yesterday = CALCULATE([Avg Temp], DATEADD('Date'[Date], -1, DAY))
RETURN IF(NOT ISBLANK(Yesterday), [Avg Temp] - Yesterday)

Avg PM2.5 = AVERAGE('current'[aqi_pm2_5])
Worst AQI Index = MAX('current'[aqi_us_epa_index])
```

### Threshold breaches — mirrors `monitoring.py`

Keep these thresholds identical to the App Settings (`ALERT_MAX_TEMP_C` etc.), so the report and
the alert rules never disagree:

```dax
Heat Breaches = CALCULATE([Observations], 'current'[temp_c] >= 38)
Cold Breaches = CALCULATE([Observations], 'current'[temp_c] <= -10)
Wind Breaches = CALCULATE([Observations], 'current'[wind_kph] >= 60)
AQI Breaches  = CALCULATE([Observations], 'current'[aqi_us_epa_index] >= 4)

Breach Rate =
DIVIDE(
    [Heat Breaches] + [Cold Breaches] + [Wind Breaches] + [AQI Breaches],
    [Observations]
)
```

### A calculated column for the AQI scale

```dax
AQI Category =
SWITCH(
    TRUE(),
    'current'[aqi_us_epa_index] <= 2, "Good",
    'current'[aqi_us_epa_index] = 3, "Unhealthy for sensitive",
    'current'[aqi_us_epa_index] = 4, "Unhealthy",
    'current'[aqi_us_epa_index] = 5, "Very unhealthy",
    "Hazardous"
)
```

Sort it by `aqi_us_epa_index` so the legend reads in severity order rather than alphabetically.

## 5. Report pages

Two pages, matching the split the whole project is built around: is the pipeline healthy, and what
is the data saying.

### Page 1 — Pipeline health

| Visual | Fields |
| --- | --- |
| Card | `Data Age (min)` — conditional formatting: red above 90 |
| Card | `Observations` |
| Card | `Avg Ingest Lag (s)` |
| Card | `Locations` |
| Line chart | X `observed_at_local` (hour), Y `Observations`, legend `location_name` — **gaps in this line are the story**: a flat stretch means ingestion stopped |
| Column chart | X hour, Y `Max Ingest Lag (s)` |
| Table | `location_name`, `Observations`, `Avg Ingest Lag (s)`, `Data Age (min)` |

### Page 2 — Weather and air quality

| Visual | Fields |
| --- | --- |
| Line chart | X `observed_at_local`, Y `Avg Temp` and `Avg Feels Like`, legend `location_name` |
| Card row | `Max Temp`, `Min Temp`, `Feels-Like Gap`, `Breach Rate` |
| Stacked column | X `Date`, Y `Observations`, legend `AQI Category` |
| Scatter | X `Avg Temp`, Y `Avg PM2.5`, detail `location_name` |
| Matrix | rows `location_name`, columns `Date`, values `Max Temp` with a colour scale |
| Slicers | `location_name`, `Date` range |

Put the four breach measures in a small card row on this page too — it is the direct visual
counterpart to the Azure Monitor alert rules.

## 6. Refresh

The lake is append-only, so **Import** mode with a scheduled refresh is right; DirectQuery over
Parquet in blob storage would be slow and buys nothing here.

A Free licence refreshes manually in Desktop. Scheduled refresh in the Service needs Pro, and
credentials for the data source — use the same account key.

## 7. What to commit

Save as **`weather.pbit`** (`File → Save As → Power BI template`), not `.pbix`. A template carries
the queries, model and visuals but no data, so the repository stays free of a data extract and the
file stays small.

Screenshots go in `../docs/images/`, referenced from the main README. A short GIF of the report
being filtered is worth more than three static shots — reviewers do not open `.pbit` files, but
they do look at pictures.
