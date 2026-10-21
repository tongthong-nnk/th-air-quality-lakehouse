"""Copy one day of OpenAQ archive files, for every station, into raw/ as a single CSV.gz.

Running it again for the same day overwrites the same object, so re-runs never duplicate data.
"""

import csv
import datetime as dt
import functools
import gzip
import io
import logging
import os
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore import UNSIGNED
from botocore.config import Config

ARCHIVE_BUCKET = "openaq-data-archive"
FIELDS = "location_id,sensors_id,location,datetime,lat,lon,parameter,units,value".split(",")
# Some archive files use the singular column names from the OpenAQ docs.
RENAME = {"sensor_id": "sensors_id", "unit": "units"}
THREADS = 16

logger = logging.getLogger(__name__)


def archive_key(location_id: int, day: dt.date) -> str:
    return (
        f"records/csv.gz/locationid={location_id}/year={day:%Y}/month={day:%m}/"
        f"location-{location_id}-{day:%Y%m%d}.csv.gz"
    )


def raw_key(day: dt.date) -> str:
    return f"raw/openaq/dt={day}/part-0000.csv.gz"


def read_station_day(archive, location_id: int, day: dt.date) -> list[list[str]] | None:
    """Rows of one station-day file in FIELDS order, or None when the file is not published yet."""
    try:
        obj = archive.get_object(Bucket=ARCHIVE_BUCKET, Key=archive_key(location_id, day))
    except archive.exceptions.NoSuchKey:
        return None
    reader = csv.reader(io.StringIO(gzip.decompress(obj["Body"].read()).decode("utf-8")))
    header = [RENAME.get(name, name) for name in next(reader, [])]
    columns = [header.index(field) if field in header else None for field in FIELDS]
    return [[row[i] if i is not None and i < len(row) else "" for i in columns] for row in reader]


def build_day_file(archive, location_ids: list[int], day: dt.date) -> tuple[bytes, dict]:
    """Read every station's file for one day in parallel and combine them into one CSV.gz.

    One file per day instead of hundreds of tiny ones keeps the Spark read fast.
    """
    stats = {"date": str(day), "stations": 0, "missing": 0, "rows": 0}
    buffer = io.BytesIO()
    # mtime=0 makes the output byte-identical when the input has not changed.
    with (
        gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as gz,
        io.TextIOWrapper(gz, encoding="utf-8", newline="") as text,
        ThreadPoolExecutor(THREADS) as pool,
    ):
        writer = csv.writer(text)
        writer.writerow(FIELDS)
        for rows in pool.map(lambda i: read_station_day(archive, i, day), sorted(location_ids)):
            if rows is None:
                stats["missing"] += 1
                continue
            stats["stations"] += 1
            stats["rows"] += len(rows)
            writer.writerows(rows)
    return buffer.getvalue(), stats


def archive_client():
    """Anonymous client for the public archive bucket, which is in us-east-1."""
    return boto3.client(
        "s3",
        region_name="us-east-1",
        config=Config(
            signature_version=UNSIGNED,
            max_pool_connections=THREADS,
            retries={"max_attempts": 5, "mode": "standard"},
        ),
    )


@functools.cache
def clients():
    """S3 clients, created once per Lambda container and reused across invocations."""
    return archive_client(), boto3.client("s3")


def handler(event, context):
    day = dt.date.fromisoformat(event["date"])
    archive, lake = clients()
    body, stats = build_day_file(archive, event["location_ids"], day)
    lake.put_object(
        Bucket=os.environ["LAKE_BUCKET"],
        Key=raw_key(day),
        Body=body,
        ContentType="application/gzip",
    )
    logger.info("ingested day", extra=stats)
    return stats
