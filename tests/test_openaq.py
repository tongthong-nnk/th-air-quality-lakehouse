import csv
import datetime as dt
import gzip
import io
import json

import boto3
import pytest

import openaq
from openaq import FIELDS, archive_key, build_day_file, raw_key

DAY = dt.date(2026, 10, 17)
HEADER = '"location_id","sensors_id","location","datetime","lat","lon","parameter","units","value"'
ROW = (
    '225642,1304146,"Bang Phlat District Office, Bangkok",{time},'
    '"13.79","100.50","pm25","µg/m³","20.8"'
)


def station_file(*lines: str) -> bytes:
    return gzip.compress("\n".join(lines).encode("utf-8"))


def read_raw(body: bytes) -> list[dict]:
    return list(csv.DictReader(io.StringIO(gzip.decompress(body).decode("utf-8"))))


@pytest.fixture
def buckets(aws, monkeypatch):
    """A fake OpenAQ archive in us-east-1 and a fake lake bucket."""
    openaq.clients.cache_clear()
    archive = boto3.client("s3", region_name="us-east-1")
    archive.create_bucket(Bucket=openaq.ARCHIVE_BUCKET)
    # Like the real archive: anyone may list and read, so the handler's unsigned client works.
    bucket_arn = f"arn:aws:s3:::{openaq.ARCHIVE_BUCKET}"
    archive.put_bucket_policy(
        Bucket=openaq.ARCHIVE_BUCKET,
        Policy=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": "*",
                        "Action": ["s3:GetObject", "s3:ListBucket"],
                        "Resource": [bucket_arn, f"{bucket_arn}/*"],
                    }
                ],
            }
        ),
    )
    lake = boto3.client("s3")
    lake.create_bucket(Bucket="lake", CreateBucketConfiguration={"LocationConstraint": aws})
    monkeypatch.setenv("LAKE_BUCKET", "lake")
    yield archive, lake
    openaq.clients.cache_clear()


def test_archive_key_matches_openaq_layout():
    key = archive_key(2178, dt.date(2022, 5, 3))
    assert key == "records/csv.gz/locationid=2178/year=2022/month=05/location-2178-20220503.csv.gz"


def test_raw_key_is_one_object_per_day():
    assert raw_key(DAY) == "raw/openaq/dt=2026-10-17/part-0000.csv.gz"


def test_day_file_combines_stations_and_skips_missing_files(buckets):
    archive, _ = buckets
    archive.put_object(
        Bucket=openaq.ARCHIVE_BUCKET,
        Key=archive_key(225642, DAY),
        Body=station_file(HEADER, ROW.format(time="2026-10-17T01:00:00+07:00")),
    )
    # Older files spell two columns differently and order them differently.
    archive.put_object(
        Bucket=openaq.ARCHIVE_BUCKET,
        Key=archive_key(1, DAY),
        Body=station_file(
            "sensor_id,location_id,location,datetime,lat,lon,parameter,unit,value",
            "11,1,Chiang Mai,2026-10-17T02:00:00+07:00,18.79,98.99,pm25,µg/m³,55.1",
        ),
    )

    body, stats = build_day_file(archive, [225642, 1, 999], DAY)

    rows = read_raw(body)
    assert stats == {"date": "2026-10-17", "stations": 2, "missing": 1, "rows": 2}
    assert list(rows[0]) == FIELDS
    assert rows[0]["sensors_id"] == "11" and rows[0]["units"] == "µg/m³"
    assert rows[1]["location"] == "Bang Phlat District Office, Bangkok"


def test_handler_writes_one_object_and_reruns_are_identical(buckets):
    archive, lake = buckets
    archive.put_object(
        Bucket=openaq.ARCHIVE_BUCKET,
        Key=archive_key(225642, DAY),
        Body=station_file(HEADER, ROW.format(time="2026-10-17T01:00:00+07:00")),
    )
    event = {"date": "2026-10-17", "location_ids": [225642, 404]}

    first = openaq.handler(event, None)
    body_first = lake.get_object(Bucket="lake", Key=raw_key(DAY))["Body"].read()
    second = openaq.handler(event, None)
    body_second = lake.get_object(Bucket="lake", Key=raw_key(DAY))["Body"].read()

    assert first == second == {"date": "2026-10-17", "stations": 1, "missing": 1, "rows": 1}
    assert body_first == body_second
    keys = [o["Key"] for o in lake.list_objects_v2(Bucket="lake")["Contents"]]
    assert keys == [raw_key(DAY)]
