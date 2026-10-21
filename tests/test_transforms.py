import json

from openaq import FIELDS
from transforms import split_hotspots, split_measurements, typed_hotspots, typed_measurements

ROW = ("1", "11", "Bangkok", "2026-01-01T01:00:00+07:00", "13.7", "100.5", "PM25", "µg/m³", "42.5")

FIRMS_FIELDS = (
    "latitude,longitude,acq_date,acq_time,satellite,confidence,frp,daynight,source".split(",")
)
FIRE = ("18.79", "98.98", "2026-01-01", "1830", "N20", "n", "3.2", "N", "VIIRS_NOAA20_NRT")


def readings(spark, *rows):
    return split_measurements(typed_measurements(spark.createDataFrame(rows, FIELDS)))


def fires(spark, *rows):
    return split_hotspots(typed_hotspots(spark.createDataFrame(rows, FIRMS_FIELDS)))


def with_time(time):
    return ROW[:3] + (time,) + ROW[4:]


def test_casts_types_and_keeps_thai_date(spark):
    good, bad = readings(spark, ROW)
    row = good.selectExpr("*", "date_format(measured_at, 'yyyy-MM-dd HH:mm') AS utc").first()
    assert bad.count() == 0
    assert (row.sensors_id, row.parameter, row.value) == (11, "pm25", 42.5)
    assert str(row.local_date) == "2026-01-01"
    assert row.utc == "2025-12-31 18:00"


def test_midnight_reading_belongs_to_the_day_it_closes(spark):
    # The 00:00 reading is the average of 23:00-24:00, so it belongs to the day before.
    good, _ = readings(spark, with_time("2026-01-02T00:00:00+07:00"))
    assert str(good.first().local_date) == "2026-01-01"


def test_removes_duplicates(spark):
    good, _ = readings(spark, ROW, ROW)
    assert good.count() == 1


def test_bad_rows_are_rejected_with_a_reason(spark):
    good, bad = readings(
        spark,
        ROW[:8] + ("-5",),
        ROW[:8] + ("",),
        ROW[:8] + ("1200",),
        with_time("not-a-time"),
        ("1", "", *ROW[2:]),
    )
    reasons = sorted(r.reject_reason for r in bad.collect())
    assert good.count() == 0
    assert reasons == [
        "missing id",
        "missing value",
        "negative value",
        "pm25 above 1000",
        "unreadable datetime",
    ]


def test_rejected_row_keeps_the_original_text(spark):
    _, bad = readings(spark, with_time("01/01/2026 01:00"))
    assert json.loads(bad.first().raw_record)["datetime"] == "01/01/2026 01:00"


def test_high_values_of_other_parameters_are_kept(spark):
    pm10 = ROW[:6] + ("pm10", "µg/m³", "1200")
    good, bad = readings(spark, pm10)
    assert (good.count(), bad.count()) == (1, 0)


def test_fire_time_is_utc_and_late_evening_utc_is_the_next_thai_day(spark):
    good, bad = fires(spark, FIRE)
    row = good.selectExpr("*", "date_format(detected_at, 'yyyy-MM-dd HH:mm') AS utc").first()
    assert bad.count() == 0
    assert row.utc == "2026-01-01 18:30"
    assert str(row.local_date) == "2026-01-02"


def test_fire_time_without_leading_zeros(spark):
    good, _ = fires(spark, FIRE[:3] + ("5",) + FIRE[4:])
    row = good.selectExpr("*", "date_format(detected_at, 'HH:mm') AS utc").first()
    assert (row.utc, str(row.local_date)) == ("00:05", "2026-01-01")


def test_bad_fire_rows_are_rejected(spark):
    good, bad = fires(spark, ("", *FIRE[1:]), FIRE[:3] + ("25:99",) + FIRE[4:])
    reasons = sorted(r.reject_reason for r in bad.collect())
    assert good.count() == 0
    assert reasons == ["missing coordinates", "unreadable time"]
