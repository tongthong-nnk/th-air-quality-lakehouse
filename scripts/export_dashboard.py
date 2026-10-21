"""Export the gold view to the JSON file that the dashboard page reads.

GitHub Actions runs this with a role that can only query the gold view through Athena.
scripts/run_local.py calls build() with rows from the local Trino instead.
"""

import argparse
import datetime as dt
import json
import pathlib
import time
from collections import defaultdict
from collections.abc import Callable

Query = Callable[[str], list[dict]]

STATIONS_SQL = """
SELECT location_id, arbitrary(station_name) AS station_name, arbitrary(locality) AS locality,
       arbitrary(latitude) AS latitude, arbitrary(longitude) AS longitude
FROM aq_gold.v_station_day
WHERE complete_day
GROUP BY location_id
ORDER BY location_id
"""

# Days with fewer than 18 hourly readings are left out: the standards are 24-hour averages.
DAYS_SQL = """
SELECT location_id, CAST(local_date AS varchar) AS local_date, pm25_avg, fires_100km,
       over_thai_std
FROM aq_gold.v_station_day
WHERE complete_day
ORDER BY location_id, local_date
"""

INTEGER_TYPES = {"tinyint", "smallint", "integer", "bigint"}
FLOAT_TYPES = {"real", "float", "double", "decimal"}


def convert(athena_type: str, value: str | None):
    """Athena returns every value as a string; turn it back into the column's type."""
    if value is None:
        return None
    if athena_type in INTEGER_TYPES:
        return int(value)
    if athena_type in FLOAT_TYPES:
        return float(value)
    if athena_type == "boolean":
        return value == "true"
    return value


def athena_query(workgroup: str, client=None, poll_seconds: float = 1) -> Query:
    if client is None:
        import boto3

        client = boto3.client("athena")

    def run(sql: str) -> list[dict]:
        query_id = client.start_query_execution(QueryString=sql, WorkGroup=workgroup)[
            "QueryExecutionId"
        ]
        while True:
            status = client.get_query_execution(QueryExecutionId=query_id)["QueryExecution"][
                "Status"
            ]
            if status["State"] not in ("QUEUED", "RUNNING"):
                break
            time.sleep(poll_seconds)
        if status["State"] != "SUCCEEDED":
            reason = status.get("StateChangeReason", "")
            raise RuntimeError(f"Athena query {query_id} {status['State']}: {reason}")

        rows, columns = [], None
        pages = client.get_paginator("get_query_results").paginate(QueryExecutionId=query_id)
        for page in pages:
            data = page["ResultSet"]["Rows"]
            if columns is None:
                columns = page["ResultSet"]["ResultSetMetadata"]["ColumnInfo"]
                data = data[1:]  # the first row of the first page holds the column names
            rows += [
                {
                    c["Name"]: convert(c["Type"], cell.get("VarCharValue"))
                    for c, cell in zip(columns, row["Data"], strict=True)
                }
                for row in data
            ]
        return rows

    return run


def monthly(days: list[dict], fires: bool) -> list[dict]:
    """One row per month across all stations, for the burning-season chart."""
    by_month = defaultdict(list)
    for row in days:
        by_month[row["local_date"][:7]].append(row)
    months = []
    for month, rows in sorted(by_month.items()):
        summary = {
            "month": month,
            "stations": len({r["location_id"] for r in rows}),
            "pm25_avg": round(sum(r["pm25_avg"] for r in rows) / len(rows), 1),
            "over_thai_pct": round(100 * sum(r["over_thai_std"] for r in rows) / len(rows), 1),
        }
        if fires:
            summary["fires_avg"] = round(sum(r["fires_100km"] for r in rows) / len(rows), 1)
        months.append(summary)
    return months


def build(stations: list[dict], days: list[dict], fires: bool = True) -> dict:
    """Shape the rows for the page: one series per station, null where a day is missing.

    Every series covers the same range of days, so the page can find any day by its index.
    Without fire data (a local run with no FIRMS key) the fire fields are left out, not zero.
    """
    if not days:
        raise RuntimeError("the gold view has no complete station-days")
    dates = [dt.date.fromisoformat(r["local_date"]) for r in days]
    first, last = min(dates), max(dates)
    size = (last - first).days + 1

    series = {s["location_id"]: {"pm25": [None] * size, "fires": [None] * size} for s in stations}
    for row, day in zip(days, dates, strict=True):
        i = (day - first).days
        series[row["location_id"]]["pm25"][i] = row["pm25_avg"]
        series[row["location_id"]]["fires"][i] = row["fires_100km"]

    output = []
    for s in stations:
        station = {
            "id": s["location_id"],
            "name": s["station_name"],
            "locality": s["locality"],
            "lat": s["latitude"],
            "lon": s["longitude"],
            "pm25": series[s["location_id"]]["pm25"],
        }
        if fires:
            station["fires"] = series[s["location_id"]]["fires"]
        output.append(station)

    return {
        "generated_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "first_day": str(first),
        "last_day": str(last),
        "has_fires": fires,
        "stations": output,
        "months": monthly(days, fires),
    }


def write(data: dict, path: pathlib.Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n")


def export(query: Query, path: pathlib.Path, fires: bool = True) -> dict:
    data = build(query(STATIONS_SQL), query(DAYS_SQL), fires)
    write(data, path)
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workgroup", default="thaq")
    parser.add_argument(
        "--out", type=pathlib.Path, default=pathlib.Path("dashboard/data/dashboard.json")
    )
    args = parser.parse_args()
    data = export(athena_query(args.workgroup), args.out)
    print(f"wrote {len(data['stations'])} stations, {data['first_day']} to {data['last_day']}")


if __name__ == "__main__":
    main()
