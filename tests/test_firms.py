import csv
import datetime as dt
import gzip
import io
import urllib.error

import boto3
import pytest

import firms

HEADER = (
    "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,instrument,"
    "confidence,version,bright_ti5,frp,daynight"
)
AVAILABILITY = (
    "data_id,min_date,max_date\n"
    "VIIRS_NOAA20_NRT,2026-07-01,2026-10-20\n"
    "VIIRS_NOAA20_SP,2018-04-01,2026-06-30\n"
)


def detection(acq_date, acq_time, lat="18.79"):
    return f"{lat},98.98,330.1,0.4,0.37,{acq_date},{acq_time},N20,VIIRS,n,2.0NRT,290.2,3.2,N"


def fake_firms(responses):
    """Stand-in for get_text that answers by the date at the end of the URL."""

    def fetch(url):
        if "/data_availability/" in url:
            return AVAILABILITY
        return responses[url.rsplit("/", 1)[1]]

    return fetch


def test_prefers_reprocessed_data_and_falls_back_to_near_real_time():
    available = firms.parse_availability(AVAILABILITY)
    assert firms.choose_source(available, "2026-06-30") == "VIIRS_NOAA20_SP"
    assert firms.choose_source(available, "2026-07-01") == "VIIRS_NOAA20_NRT"
    with pytest.raises(RuntimeError, match="no VIIRS_NOAA20 data for 2026-10-21"):
        firms.choose_source(available, "2026-10-21")


def test_thai_date_of_a_detection():
    assert firms.thai_date({"acq_date": "2026-10-16", "acq_time": "1830"}) == "2026-10-17"
    assert firms.thai_date({"acq_date": "2026-10-16", "acq_time": "5"}) == "2026-10-16"
    assert firms.thai_date({"acq_date": "2026-10-16", "acq_time": "late"}) is None


def test_one_thai_day_is_built_from_two_utc_dates(monkeypatch):
    monkeypatch.setattr(
        firms,
        "get_text",
        fake_firms(
            {
                # 18:30 UTC on the 15th is 01:30 on the 16th in Thailand: keep.
                "2026-10-15": f"{HEADER}\n{detection('2026-10-15', '1830')}\n"
                f"{detection('2026-10-15', '0630')}\n",
                # 06:30 UTC on the 16th is 13:30 in Thailand: keep. 18:00 UTC is the 17th: drop.
                "2026-10-16": f"{HEADER}\n{detection('2026-10-16', '630')}\n"
                f"{detection('2026-10-16', '1800')}\n",
            }
        ),
    )
    available = firms.parse_availability(AVAILABILITY)

    rows, sources = firms.detections_for_day("key", available, dt.date(2026, 10, 16))

    assert [(r["acq_date"], r["acq_time"]) for r in rows] == [
        ("2026-10-15", "1830"),
        ("2026-10-16", "630"),
    ]
    assert sources == ["VIIRS_NOAA20_NRT", "VIIRS_NOAA20_NRT"]


def test_text_instead_of_csv_is_an_error():
    with pytest.raises(RuntimeError, match="Invalid MAP_KEY"):
        firms.read_detections("Invalid MAP_KEY.", "VIIRS_NOAA20_NRT", "2026-10-16")


def test_errors_do_not_leak_the_map_key(monkeypatch):
    def refuse(url, timeout):
        raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(firms.urllib.request, "urlopen", refuse)
    with pytest.raises(RuntimeError) as error:
        firms.get_text(f"{firms.API}/area/csv/SECRET-KEY/VIIRS_NOAA20_NRT/1,2,3,4/1/2026-10-16")
    assert "SECRET-KEY" not in str(error.value)
    assert "403" in str(error.value)


def test_handler_writes_one_file_per_thai_day(aws, monkeypatch):
    boto3.client("ssm").put_parameter(
        Name="/thaq/firms-map-key", Value="secret", Type="SecureString"
    )
    boto3.client("s3").create_bucket(
        Bucket="lake", CreateBucketConfiguration={"LocationConstraint": aws}
    )
    monkeypatch.setenv("MAP_KEY_PARAM", "/thaq/firms-map-key")
    monkeypatch.setenv("LAKE_BUCKET", "lake")
    firms.availability.cache_clear()
    monkeypatch.setattr(
        firms,
        "get_text",
        fake_firms(
            {
                "2026-10-15": f"{HEADER}\n",
                "2026-10-16": f"{HEADER}\n{detection('2026-10-16', '0630')}\n",
            }
        ),
    )

    out = firms.handler({"date": "2026-10-16"}, None)

    body = boto3.client("s3").get_object(Bucket="lake", Key=firms.raw_key(dt.date(2026, 10, 16)))
    rows = list(csv.DictReader(io.StringIO(gzip.decompress(body["Body"].read()).decode())))
    assert out == {
        "date": "2026-10-16",
        "detections": 1,
        "sources": ["VIIRS_NOAA20_NRT", "VIIRS_NOAA20_NRT"],
    }
    assert rows[0]["source"] == "VIIRS_NOAA20_NRT"
    assert list(rows[0]) == [*firms.FIELDS, "source"]
