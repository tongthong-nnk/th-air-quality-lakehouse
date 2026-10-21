"""Download one day of NASA FIRMS fire detections around Thailand into raw/ as a single CSV.gz.

FIRMS reports acquisition times in UTC, and Thailand is UTC+7, so one Thai day spans two UTC
dates: the night overpass of day D is on UTC date D-1. The handler requests both UTC dates and
keeps the detections that fall on day D in Thai time. Running it again overwrites the same file.
"""

import csv
import datetime as dt
import functools
import gzip
import io
import logging
import os
import urllib.error
import urllib.request

import boto3

API = "https://firms.modaps.eosdis.nasa.gov/api"
SATELLITE = "VIIRS_NOAA20"
# West, south, east, north: Thailand plus about 100 km on every side, the radius gold uses.
AREA = "96,4,107,22"
FIELDS = (
    "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,instrument,"
    "confidence,version,bright_ti5,frp,daynight"
).split(",")
REQUIRED = {"latitude", "longitude", "acq_date", "acq_time", "confidence"}
THAI_OFFSET = dt.timedelta(hours=7)

logger = logging.getLogger(__name__)


def raw_key(day: dt.date) -> str:
    return f"raw/firms/dt={day}/part-0000.csv.gz"


def get_text(url: str) -> str:
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            return response.read().decode("utf-8")
    except urllib.error.URLError as error:
        # The URL holds the MAP_KEY, so it must not reach the logs or the alert email.
        status = getattr(error, "code", None) or "no response"
        raise RuntimeError(f"FIRMS request failed ({status}): {error.reason}") from None


def parse_availability(text: str) -> dict[str, tuple[str, str]]:
    return {r["data_id"]: (r["min_date"], r["max_date"]) for r in csv.DictReader(io.StringIO(text))}


@functools.lru_cache(maxsize=1)
def availability(map_key: str, hour: str) -> dict[str, tuple[str, str]]:
    """Date range of every FIRMS product. `hour` is only there to expire the cache hourly.

    One Lambda container usually serves several days of a run, and every FIRMS call counts
    against the MAP_KEY's limit of 5,000 transactions per 10 minutes.
    """
    return parse_availability(get_text(f"{API}/data_availability/csv/{map_key}/all"))


def choose_source(available: dict[str, tuple[str, str]], utc_day: str) -> str:
    """Pick the product for one UTC date.

    Standard processing (SP) replaces near-real-time (NRT) data a few months later, so prefer SP
    and use NRT for recent days. ISO dates compare correctly as strings.
    """
    for data_id in (f"{SATELLITE}_SP", f"{SATELLITE}_NRT"):
        first, last = available.get(data_id, ("", ""))
        if first <= utc_day <= last:
            return data_id
    raise RuntimeError(f"FIRMS has no {SATELLITE} data for {utc_day} yet")


def thai_date(row: dict) -> str | None:
    """Day in Thailand of one detection, or None when its date or time cannot be read."""
    try:
        hhmm = int(row["acq_time"])
        utc = dt.datetime.strptime(f"{row['acq_date']} {hhmm:04d}", "%Y-%m-%d %H%M")
    except (KeyError, TypeError, ValueError):
        return None
    return str((utc + THAI_OFFSET).date())


def read_detections(text: str, data_id: str, utc_day: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text))
    missing = REQUIRED - set(reader.fieldnames or [])
    if missing:
        # FIRMS answers some errors (bad key, limit reached) with HTTP 200 and a line of text.
        raise RuntimeError(f"unexpected FIRMS response for {data_id} {utc_day}: {text[:120]!r}")
    return [{**row, "source": data_id} for row in reader]


def detections_for_day(map_key: str, available: dict, day: dt.date) -> tuple[list[dict], list]:
    """Detections on Thai day `day` and the FIRMS products they came from."""
    rows, sources = [], []
    for utc_day in (str(day - dt.timedelta(days=1)), str(day)):
        data_id = choose_source(available, utc_day)
        text = get_text(f"{API}/area/csv/{map_key}/{data_id}/{AREA}/1/{utc_day}")
        rows += read_detections(text, data_id, utc_day)
        sources.append(data_id)
    # A row with an unreadable time stays in the file of its own date, where silver rejects it.
    return [r for r in rows if (thai_date(r) or r.get("acq_date")) == str(day)], sources


def to_csv_gz(rows: list[dict]) -> bytes:
    buffer = io.BytesIO()
    # mtime=0 makes the output byte-identical when the input has not changed.
    with (
        gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as gz,
        io.TextIOWrapper(gz, encoding="utf-8", newline="") as text,
    ):
        writer = csv.DictWriter(text, fieldnames=[*FIELDS, "source"], extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return buffer.getvalue()


def handler(event, context):
    day = dt.date.fromisoformat(event["date"])
    map_key = boto3.client("ssm").get_parameter(
        Name=os.environ["MAP_KEY_PARAM"], WithDecryption=True
    )["Parameter"]["Value"]
    hour = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H")
    rows, sources = detections_for_day(map_key, availability(map_key, hour), day)
    boto3.client("s3").put_object(
        Bucket=os.environ["LAKE_BUCKET"],
        Key=raw_key(day),
        Body=to_csv_gz(rows),
        ContentType="application/gzip",
    )
    stats = {"date": str(day), "detections": len(rows), "sources": sources}
    logger.info("ingested fires", extra=stats)
    return stats
