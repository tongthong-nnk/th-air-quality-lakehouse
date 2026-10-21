import csv
import datetime as dt
import gzip
import io

import pytest

import firms
import silver
from openaq import FIELDS

OPENAQ_ROW = ("1", "11", "Bangkok", "{time}", "13.7", "100.5", "pm25", "µg/m³", "{value}")


def day_of_readings(day, value="20.0"):
    """24 hourly readings for one Thai day, stamped 01:00 to 24:00 like the OpenAQ archive."""
    start = dt.datetime.fromisoformat(f"{day}T00:00:00+07:00")
    stamps = [(start + dt.timedelta(hours=h)).isoformat() for h in range(1, 25)]
    return [[v.format(time=stamp, value=value) for v in OPENAQ_ROW] for stamp in stamps]


def detection(acq_date, acq_time, lat="18.79"):
    values = dict.fromkeys(firms.FIELDS, "")
    values.update(latitude=lat, longitude="98.98", acq_date=acq_date, acq_time=acq_time)
    values.update(satellite="N20", confidence="n", frp="3.2", daynight="D")
    return [*values.values(), "VIIRS_NOAA20_NRT"]


def write_raw(lake, source, day, header, rows):
    path = lake / "raw" / source / f"dt={day}" / "part-0000.csv.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = io.StringIO()
    writer = csv.writer(text)
    writer.writerow(header)
    writer.writerows(rows)
    path.write_bytes(gzip.compress(text.getvalue().encode("utf-8")))


def write_openaq(lake, day, rows):
    write_raw(lake, "openaq", day, FIELDS, rows)


def write_firms(lake, day, rows):
    write_raw(lake, "firms", day, [*firms.FIELDS, "source"], rows)


@pytest.fixture
def lake(spark, tmp_path):
    for table in ("measurements", "fire_hotspots", "rejected_rows"):
        spark.sql(f"DROP TABLE IF EXISTS local.aq_silver.{table} PURGE")
    return tmp_path


def run(spark, lake, check_quality=None):
    return silver.run(spark, f"file://{lake}", "2026-10-15", "2026-10-16", "local", check_quality)


def table(spark, name):
    return spark.table(f"local.aq_silver.{name}")


def write_two_days(lake):
    negative = [*day_of_readings("2026-10-15")[0][:8], "-3"]
    write_openaq(lake, "2026-10-15", [*day_of_readings("2026-10-15"), negative])
    write_openaq(lake, "2026-10-16", day_of_readings("2026-10-16"))
    # 19:00 UTC on the 14th is 02:00 on the 15th in Thailand.
    write_firms(
        lake, "2026-10-15", [detection("2026-10-14", "1900"), detection("2026-10-15", "630")]
    )
    write_firms(
        lake, "2026-10-16", [detection("2026-10-16", "640"), detection("2026-10-16", "641", lat="")]
    )


def test_loads_both_sources_and_a_rerun_changes_nothing(spark, lake):
    write_two_days(lake)

    counts = run(spark, lake)
    first = sorted(table(spark, "measurements").collect())
    run(spark, lake)

    assert counts["openaq"] == {"readings": 49, "loaded": 48, "rejected": 1}
    assert counts["firms"] == {"detections": 4, "loaded": 3, "rejected": 1}
    assert sorted(table(spark, "measurements").collect()) == first
    assert table(spark, "fire_hotspots").count() == 3
    reasons = sorted((r.source, r.reject_reason) for r in table(spark, "rejected_rows").collect())
    assert reasons == [("firms", "missing coordinates"), ("openaq", "negative value")]


def test_rerun_picks_up_corrections_and_replaces_whole_fire_days(spark, lake):
    write_two_days(lake)
    run(spark, lake)

    corrected = day_of_readings("2026-10-15")
    corrected[4][-1] = "55.5"
    write_openaq(lake, "2026-10-15", corrected)
    write_firms(lake, "2026-10-15", [detection("2026-10-15", "630")])
    write_firms(lake, "2026-10-16", [])
    run(spark, lake)

    values = table(spark, "measurements").where("local_date = '2026-10-15'").select("value")
    assert sorted({r.value for r in values.collect()}) == [20.0, 55.5]
    fires = table(spark, "fire_hotspots").groupBy("local_date").count().collect()
    assert {str(r.local_date): r["count"] for r in fires} == {"2026-10-15": 1}
    assert table(spark, "rejected_rows").count() == 0


def test_a_day_without_a_raw_file_fails_the_run(spark, lake):
    write_openaq(lake, "2026-10-15", day_of_readings("2026-10-15"))

    with pytest.raises(RuntimeError, match="no raw file .* 2026-10-16"):
        run(spark, lake)
    assert table(spark, "measurements").count() == 0


def test_failed_quality_rules_stop_the_merge_but_keep_rejected_rows(spark, lake):
    write_two_days(lake)

    with pytest.raises(RuntimeError, match="data quality rules failed"):
        run(spark, lake, check_quality=lambda batch: ['Completeness "value" >= 0.95'])

    assert table(spark, "measurements").count() == 0
    assert table(spark, "rejected_rows").count() == 1
