# Power BI

The web dashboard and the Power BI report deliberately do different jobs.

| | Web dashboard | Power BI |
| --- | --- | --- |
| Question it answers | "What is happening right now?" | "What has been happening, and how does it compare?" |
| Latency | ~1 minute | Scheduled refresh, up to 8×/day on a Pro licence |
| Audience | Anyone with the link | Whoever the report is shared with |
| Source | `serving/*.json` via the HTTP API | `silver/` Parquet, direct from the lake |

Splitting them is not a compromise. Real-time operational monitoring and analytical reporting have
different refresh needs, different aggregations, and different consumers — which is exactly the
split between a log dashboard and a BI report in the system this project reproduces.

## Connecting to the lake

Power BI Desktop is free.

1. **Get data → Azure → Azure Data Lake Storage Gen2**
2. URL: `https://<lake account>.dfs.core.windows.net/silver`
   (find it with `az storage account list -g rg-weather-streaming --query "[?contains(name,'lake')].name" -o tsv`)
3. Authenticate with **Organizational account** — the same account that has
   `Storage Blob Data Reader` or higher on the account.
4. In the navigator, choose **Combine → Combine & Transform**. Power Query reads the
   `date=YYYY-MM-DD` folders as a partition column automatically.
5. Set types: `observed_at_utc` and `ingested_at_utc` to DateTime, the measures to Decimal.

If the account is not reachable from Desktop, grant yourself the data-plane role — subscription
`Owner` does not include it:

```bash
LAKE=$(az storage account list -g rg-weather-streaming --query "[?contains(name,'lake')].id" -o tsv)
az role assignment create --assignee "$(az ad signed-in-user show --query id -o tsv)" \
  --role "Storage Blob Data Reader" --scope "$LAKE"
```

## Suggested model

The silver table is already flat and de-duplicated, so no star schema is required for a report this
size. Add a date table and mark it as such:

```dax
Date = CALENDAR(MIN('current'[observed_at_utc]), MAX('current'[observed_at_utc]))
```

Measures worth defining:

```dax
Avg Temp = AVERAGE('current'[temp_c])
Max Temp = MAX('current'[temp_c])
Temp Δ vs Yesterday =
    [Avg Temp] - CALCULATE([Avg Temp], DATEADD('Date'[Date], -1, DAY))
Observations = DISTINCTCOUNT('current'[record_id])
Breach Rate =
    DIVIDE(
        CALCULATE([Observations], 'current'[temp_c] >= 38),
        [Observations]
    )
```

`DISTINCTCOUNT` on `record_id` rather than a plain row count is the point: the id is deterministic,
so it counts distinct observations even if a partition were ever loaded twice.

## Publishing

Power BI Service requires a **work or school account** — personal addresses such as `outlook.com`
are not accepted at sign-up. An Entra ID tenant account works.

| Goal | Requirement |
| --- | --- |
| Build a report, export `.pbix` / `.pbit` | Power BI Desktop, free |
| Publish to a workspace, share | Power BI Pro |
| Public "Publish to web" link | Pro, tenant setting enabled — **reports only, not dashboards** |
| Real-time push dataset | Pro to share; the tiles live on a dashboard |
| Public **and** real-time | Embedded / Fabric capacity — an order of magnitude more expensive |

The last row is the one that catches people out: real-time tiles only exist on dashboards, and
"Publish to web" only works on reports. There is no route to a public real-time Power BI view
without paid capacity — which is precisely why the live view is a Static Web App and Power BI covers
the analytical side.

Visual Studio Enterprise subscriptions have historically included a Power BI Pro licence; check
[my.visualstudio.com](https://my.visualstudio.com) → Benefits before paying for one.

## Files

Save the report here as `weather.pbit` (template, no data) rather than `.pbix`, so the repository
does not carry a data extract, and add screenshots to `docs/images/`.
