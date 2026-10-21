"""Load one window of raw days into the silver Iceberg tables.

The Glue job (silver_job.py) and scripts/run_local.py both call run(); only the catalog, the
lake location and the data quality check differ.
"""

import datetime as dt
from collections.abc import Callable

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F

from transforms import (
    HOTSPOTS_DDL,
    MEASUREMENTS_DDL,
    REJECTED_DDL,
    expire_old_snapshots,
    in_window,
    merge_measurements,
    replace_rows,
    split_hotspots,
    split_measurements,
    typed_hotspots,
    typed_measurements,
)

SNAPSHOT_DAYS = 7

QualityCheck = Callable[[DataFrame], list[str]]


def days_between(start: str, end: str) -> list[str]:
    first, last = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    return [str(first + dt.timedelta(days=i)) for i in range((last - first).days + 1)]


def read_raw_window(spark: SparkSession, path: str, start: str, end: str) -> DataFrame:
    """Raw CSV rows for the window. Fails if a day has no file instead of loading a partial window.

    Ingest writes a file for every day, even one with only a header, so a missing file means that
    step did not run. Reading the files as text sees header-only files too.
    """
    found = (
        spark.read.text(path)
        .where(in_window("dt", start, end))
        .select(F.col("dt").cast("string"))
        .distinct()
        .collect()
    )
    missing = sorted(set(days_between(start, end)) - {row[0] for row in found})
    if missing:
        raise RuntimeError(f"no raw file under {path} for {', '.join(missing)}")
    return spark.read.option("header", True).csv(path).where(in_window("dt", start, end))


def load_measurements(
    spark: SparkSession,
    lake: str,
    start: str,
    end: str,
    catalog: str,
    check_quality: QualityCheck | None,
) -> dict:
    # Cached so that the rejected rows, the quality check and the MERGE all see the same batch.
    readings = typed_measurements(read_raw_window(spark, f"{lake}/raw/openaq/", start, end)).cache()
    try:
        good, bad = split_measurements(readings)
        # Written before the quality gate, so a failed run still shows which rows were the problem.
        replace_rows(
            bad,
            f"{catalog}.aq_silver.rejected_rows",
            (F.col("source") == "openaq") & in_window("source_date", start, end),
        )
        failed = check_quality(readings) if check_quality else []
        if failed:
            raise RuntimeError(f"data quality rules failed: {failed}")
        merge_measurements(spark, good, f"{catalog}.aq_silver.measurements")
        return {"readings": readings.count(), "loaded": good.count(), "rejected": bad.count()}
    finally:
        readings.unpersist()


def load_hotspots(spark: SparkSession, lake: str, start: str, end: str, catalog: str) -> dict:
    detections = typed_hotspots(read_raw_window(spark, f"{lake}/raw/firms/", start, end)).cache()
    try:
        fires, bad = split_hotspots(detections)
        replace_rows(
            fires, f"{catalog}.aq_silver.fire_hotspots", in_window("local_date", start, end)
        )
        replace_rows(
            bad,
            f"{catalog}.aq_silver.rejected_rows",
            (F.col("source") == "firms") & in_window("source_date", start, end),
        )
        return {"detections": detections.count(), "loaded": fires.count(), "rejected": bad.count()}
    finally:
        detections.unpersist()


def run(
    spark: SparkSession,
    lake: str,
    start: str,
    end: str,
    catalog: str,
    check_quality: QualityCheck | None = None,
) -> dict:
    """Rebuild silver for the days start..end (inclusive) and return row counts for the log."""
    tables = {
        "measurements": MEASUREMENTS_DDL,
        "fire_hotspots": HOTSPOTS_DDL,
        "rejected_rows": REJECTED_DDL,
    }
    for name, ddl in tables.items():
        spark.sql(ddl.format(table=f"{catalog}.aq_silver.{name}"))

    counts = {
        "start_date": start,
        "end_date": end,
        "openaq": load_measurements(spark, lake, start, end, catalog, check_quality),
        "firms": load_hotspots(spark, lake, start, end, catalog),
    }
    for name in tables:
        expire_old_snapshots(spark, f"{catalog}.aq_silver.{name}", SNAPSHOT_DAYS)
    return counts
