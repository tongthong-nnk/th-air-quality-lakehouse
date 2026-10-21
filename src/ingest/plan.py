"""Plan one pipeline run: which days to ingest and which stations to read.

The result feeds the Step Functions Map state, which starts one Ingest task per day.
"""

import datetime as dt
import json
import logging
import os
import urllib.parse
import urllib.request

import boto3

API_URL = "https://api.openaq.org/v3/locations"
PM25_PARAMETER_ID = 2
PAGE_SIZE = 1000
MAX_PAGES = 10
MAX_WINDOW_DAYS = 366
LOCATIONS_KEY = "raw/openaq_locations/locations.jsonl"
BANGKOK = dt.timezone(dt.timedelta(hours=7))

logger = logging.getLogger(__name__)


def date_window(today: dt.date, start: str | None = None, end: str | None = None) -> list[str]:
    """Days to (re)ingest, oldest first.

    The default is R-7..R-4 (R = run date in Thai time). OpenAQ writes each day's archive file
    about 72 hours after the day ends, so R-4 is the newest day available, and the three older
    days pick up files that arrived late or were corrected.
    """
    first = dt.date.fromisoformat(start) if start else today - dt.timedelta(days=7)
    last = dt.date.fromisoformat(end) if end else today - dt.timedelta(days=4)
    if last < first:
        raise ValueError(f"end_date {last} is before start_date {first}")
    days = (last - first).days + 1
    if days > MAX_WINDOW_DAYS:
        raise ValueError(f"{days} days requested, at most {MAX_WINDOW_DAYS} per run")
    return [str(first + dt.timedelta(days=i)) for i in range(days)]


def is_active(location: dict, since: str) -> bool:
    """False for stations whose last reading is older than the first day of the window."""
    last = (location.get("datetimeLast") or {}).get("utc") or ""
    return not last or last[:10] >= since


def station_row(location: dict) -> dict:
    """The fields of an OpenAQ location that the gold view joins on."""
    coordinates = location.get("coordinates") or {}
    return {
        "id": location["id"],
        "name": location.get("name"),
        "locality": location.get("locality"),
        "lat": coordinates.get("latitude"),
        "lon": coordinates.get("longitude"),
        "provider": (location.get("provider") or {}).get("name"),
    }


def get_json(url: str, api_key: str) -> dict:
    request = urllib.request.Request(url, headers={"X-API-Key": api_key})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def fetch_locations(api_key: str, fetch=None) -> list[dict]:
    """All reference monitors in Thailand that measure PM2.5, following pagination.

    Low-cost air sensors are left out: their readings need corrections before they can be
    compared with the national standard.
    """
    fetch = fetch or get_json
    locations = []
    for page in range(1, MAX_PAGES + 1):
        query = urllib.parse.urlencode(
            {
                "iso": "TH",
                "parameters_id": PM25_PARAMETER_ID,
                "monitor": "true",
                "limit": PAGE_SIZE,
                "page": page,
            }
        )
        results = fetch(f"{API_URL}?{query}", api_key)["results"]
        locations += results
        if len(results) < PAGE_SIZE:
            return locations
    raise RuntimeError(f"OpenAQ returned more than {MAX_PAGES} pages of locations")


def handler(event, context):
    dates = date_window(
        dt.datetime.now(BANGKOK).date(), event.get("start_date"), event.get("end_date")
    )
    api_key = boto3.client("ssm").get_parameter(
        Name=os.environ["API_KEY_PARAM"], WithDecryption=True
    )["Parameter"]["Value"]
    stations = [station_row(loc) for loc in fetch_locations(api_key) if is_active(loc, dates[0])]
    if not stations:
        raise RuntimeError("OpenAQ returned no active PM2.5 monitors in Thailand")

    body = "".join(json.dumps(s, ensure_ascii=False) + "\n" for s in stations)
    boto3.client("s3").put_object(
        Bucket=os.environ["LAKE_BUCKET"],
        Key=LOCATIONS_KEY,
        Body=body.encode("utf-8"),
        ContentType="application/x-ndjson",
    )
    logger.info(
        "planned run",
        extra={"stations": len(stations), "start_date": dates[0], "end_date": dates[-1]},
    )
    return {
        "location_ids": sorted(s["id"] for s in stations),
        "dates": dates,
        "start_date": dates[0],
        "end_date": dates[-1],
    }
