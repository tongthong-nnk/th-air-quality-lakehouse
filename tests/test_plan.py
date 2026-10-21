import datetime as dt
import json

import boto3
import pytest

import plan
from plan import date_window, fetch_locations, is_active, station_row


def location(location_id, last_utc="2026-10-19T00:00:00Z", **extra):
    return {
        "id": location_id,
        "name": f"Station {location_id}",
        "locality": "Bangkok",
        "coordinates": {"latitude": 13.7, "longitude": 100.5},
        "datetimeLast": {"utc": last_utc} if last_utc else None,
        "provider": {"name": "Air4Thai"},
        **extra,
    }


def test_default_window_is_r7_to_r4():
    days = date_window(dt.date(2026, 10, 20))
    assert days == ["2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16"]


def test_backfill_window():
    days = date_window(dt.date(2026, 10, 20), "2026-10-01", "2026-10-03")
    assert days == ["2026-10-01", "2026-10-02", "2026-10-03"]


def test_window_rejects_reversed_dates():
    with pytest.raises(ValueError, match="before"):
        date_window(dt.date(2026, 10, 20), "2026-10-05", "2026-10-01")


def test_window_rejects_bad_dates():
    with pytest.raises(ValueError):
        date_window(dt.date(2026, 10, 20), "2026-13-01")


def test_window_has_an_upper_limit():
    with pytest.raises(ValueError, match="at most"):
        date_window(dt.date(2026, 10, 20), "2016-10-01", "2026-10-01")


def test_stations_that_stopped_reporting_are_skipped():
    assert is_active(location(1, "2026-10-19T00:00:00Z"), since="2026-10-13")
    assert not is_active(location(2, "2025-01-01T00:00:00Z"), since="2026-10-13")
    assert is_active(location(3, last_utc=None), since="2026-10-13")


def test_station_row_keeps_the_fields_the_view_needs():
    assert station_row(location(7)) == {
        "id": 7,
        "name": "Station 7",
        "locality": "Bangkok",
        "lat": 13.7,
        "lon": 100.5,
        "provider": "Air4Thai",
    }


def test_fetch_locations_follows_pages():
    calls = []

    def fake_fetch(url, api_key):
        calls.append(url)
        page = len(calls)
        size = plan.PAGE_SIZE if page == 1 else 3
        return {"results": [location(page * 10_000 + i) for i in range(size)]}

    locations = fetch_locations("key", fetch=fake_fetch)

    assert len(locations) == plan.PAGE_SIZE + 3
    assert "page=2" in calls[1]
    assert all("iso=TH" in url and "parameters_id=2" in url for url in calls)
    assert all("monitor=true" in url for url in calls), "low-cost sensors must be left out"


def test_fetch_locations_stops_if_pages_never_end():
    def endless(url, api_key):
        return {"results": [location(i) for i in range(plan.PAGE_SIZE)]}

    with pytest.raises(RuntimeError, match="pages"):
        fetch_locations("key", fetch=endless)


def test_handler_writes_stations_and_returns_the_map_input(aws, monkeypatch):
    boto3.client("ssm").put_parameter(
        Name="/thaq/openaq-api-key", Value="secret", Type="SecureString"
    )
    boto3.client("s3").create_bucket(
        Bucket="lake", CreateBucketConfiguration={"LocationConstraint": aws}
    )
    monkeypatch.setenv("API_KEY_PARAM", "/thaq/openaq-api-key")
    monkeypatch.setenv("LAKE_BUCKET", "lake")
    seen_keys = []

    def fake_fetch(url, api_key):
        seen_keys.append(api_key)
        return {"results": [location(5), location(3), location(9, "2020-01-01T00:00:00Z")]}

    monkeypatch.setattr(plan, "get_json", fake_fetch)

    out = plan.handler({"start_date": "2026-10-01", "end_date": "2026-10-02"}, None)

    assert out == {
        "location_ids": [3, 5],
        "dates": ["2026-10-01", "2026-10-02"],
        "start_date": "2026-10-01",
        "end_date": "2026-10-02",
    }
    assert seen_keys == ["secret"]
    body = boto3.client("s3").get_object(Bucket="lake", Key=plan.LOCATIONS_KEY)["Body"].read()
    rows = [json.loads(line) for line in body.decode().splitlines()]
    assert [r["id"] for r in rows] == [5, 3]
