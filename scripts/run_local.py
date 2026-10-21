"""Run the whole pipeline on this machine, without AWS.

    make local-up
    make local-run ARGS="--start 2026-10-01 --end 2026-10-17"

Each step runs the same code as in AWS: openaq.py and firms.py write raw days to .local/lake,
silver.py loads them with local Spark into Iceberg tables in the REST catalog from compose.yaml,
and Trino runs the gold SQL that Athena runs. The last step writes the dashboard JSON.

Stations come from the OpenAQ API (set OPENAQ_API_KEY) or from --locations, a JSON lines file in
the format the Plan Lambda writes. Fire detections need a FIRMS MAP_KEY (FIRMS_MAP_KEY). Without
one the run still works, and the dashboard leaves the fire fields out instead of showing zeros.
"""

import argparse
import datetime as dt
import functools
import json
import os
import pathlib
import queue
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import export_dashboard
import firms
import local_trino
import openaq
import plan
import silver

# Same Iceberg release as AWS Glue 5.1.
ICEBERG_PACKAGE = "org.apache.iceberg:iceberg-spark-runtime-3.5_2.12:1.10.0"
DAY_WORKERS = 4


def load_stations(args) -> list[dict]:
    if args.locations:
        lines = args.locations.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]
    api_key = os.environ.get("OPENAQ_API_KEY")
    if not api_key:
        sys.exit("Set OPENAQ_API_KEY or pass --locations.")
    locations = plan.fetch_locations(api_key)
    return [plan.station_row(loc) for loc in locations if plan.is_active(loc, args.start)]


def write(path: pathlib.Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


def ingest(lake: pathlib.Path, location_ids: list[int], days: list[str], map_key: str | None):
    """Write the raw files for each day, several days at a time."""
    # One archive client per worker: each keeps its own pool of 16 connections.
    clients = queue.Queue()
    for _ in range(DAY_WORKERS):
        clients.put(openaq.archive_client())
    hour = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H")
    available = firms.availability(map_key, hour) if map_key else None

    def one_day(day: str) -> dict:
        date = dt.date.fromisoformat(day)
        archive = clients.get()
        try:
            body, stats = openaq.build_day_file(archive, location_ids, date)
        finally:
            clients.put(archive)
        write(lake / openaq.raw_key(date), body)
        # Silver expects a FIRMS file for every day, so write an empty one without a key.
        fires = firms.detections_for_day(map_key, available, date)[0] if map_key else []
        write(lake / firms.raw_key(date), firms.to_csv_gz(fires))
        return {**stats, "fires": len(fires)}

    totals = {"rows": 0, "fires": 0}
    with ThreadPoolExecutor(DAY_WORKERS) as pool:
        for i, stats in enumerate(pool.map(one_day, days), start=1):
            totals["rows"] += stats["rows"]
            totals["fires"] += stats["fires"]
            if i % 30 == 0 or i == len(days):
                print(f"  raw: {i}/{len(days)} days, {totals['rows']:,} readings", flush=True)
    return totals


def spark_session():
    from pyspark.sql import SparkSession

    return (
        SparkSession.builder.master("local[*]")
        .appName("thaq-local")
        .config("spark.jars.packages", ICEBERG_PACKAGE)
        .config(
            "spark.sql.extensions",
            "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions",
        )
        .config("spark.sql.catalog.lake", "org.apache.iceberg.spark.SparkCatalog")
        .config("spark.sql.catalog.lake.type", "rest")
        .config("spark.sql.catalog.lake.uri", "http://localhost:8181")
        .config("spark.sql.session.timeZone", "UTC")
        .config("spark.driver.memory", "4g")
        .config("spark.ui.enabled", "false")
        .getOrCreate()
    )


def windows(days: list[str]) -> list[tuple[str, str]]:
    """Split the days the way a backfill in AWS does: at most one Step Functions run per year."""
    size = plan.MAX_WINDOW_DAYS
    return [(days[i], days[min(i + size, len(days)) - 1]) for i in range(0, len(days), size)]


def build_gold(conn, stations: list[dict]) -> tuple[int, str]:
    local_trino.create_tables(conn)
    # Plan rewrites the locations file every run; locally the table plays that file's part.
    local_trino.query(conn, "DELETE FROM aq_silver.locations")
    columns = ["id", "name", "locality", "lat", "lon", "provider"]
    values = [
        "(" + ", ".join(local_trino.literal(s.get(c)) for c in columns) + ")" for s in stations
    ]
    local_trino.query(conn, f"INSERT INTO aq_silver.locations VALUES {', '.join(values)}")
    local_trino.run_file(conn, "gold/dim_station.sql")
    local_trino.run_file(conn, "gold/fct_station_day.sql")
    [[failed, details]] = local_trino.run_file(conn, "gold/checks.sql")
    return failed, details


def main() -> None:
    today = dt.datetime.now(plan.BANGKOK).date()
    default_days = plan.date_window(today)
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--start", default=default_days[0], help="first day, YYYY-MM-DD")
    parser.add_argument("--end", default=default_days[-1], help="last day, YYYY-MM-DD")
    parser.add_argument("--locations", type=pathlib.Path, help="stations as JSON lines")
    parser.add_argument("--lake", type=pathlib.Path, default=pathlib.Path(".local/lake"))
    parser.add_argument(
        "--out", type=pathlib.Path, default=pathlib.Path("dashboard/data/dashboard.json")
    )
    parser.add_argument("--skip-ingest", action="store_true", help="reuse the raw files")
    args = parser.parse_args()

    started = time.monotonic()
    days = silver.days_between(args.start, args.end)
    stations = load_stations(args)
    map_key = os.environ.get("FIRMS_MAP_KEY")
    print(f"{len(days)} days, {len(stations)} stations, fires: {'yes' if map_key else 'no key'}")

    if not args.skip_ingest:
        write(
            args.lake / plan.LOCATIONS_KEY,
            "".join(json.dumps(s, ensure_ascii=False) + "\n" for s in stations).encode(),
        )
        ingest(args.lake, sorted(s["id"] for s in stations), days, map_key)

    spark = spark_session()
    spark.sql("CREATE NAMESPACE IF NOT EXISTS lake.aq_silver")
    for start, end in windows(days):
        counts = silver.run(spark, args.lake.resolve().as_uri(), start, end, "lake")
        print(f"  silver: {json.dumps(counts)}", flush=True)
    spark.stop()

    conn = local_trino.connect()
    failed, details = build_gold(conn, stations)
    print(f"  gold checks: {failed} failed ({details})")

    query = functools.partial(local_trino.records, conn)
    data = export_dashboard.export(query, args.out, fires=bool(map_key))
    print(
        f"wrote {args.out}: {len(data['stations'])} stations, "
        f"{data['first_day']} to {data['last_day']}, in {time.monotonic() - started:.0f} s"
    )


if __name__ == "__main__":
    main()
