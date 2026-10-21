"""The gold SQL in sql/, run on Trino from compose.yaml (start it with `make local-up`)."""

import datetime as dt
import os

import pytest
import trino

import local_trino

# Separate schemas, so running the tests does not touch tables from scripts/run_local.py.
SCHEMAS = {"aq_silver": "test_silver", "aq_gold": "test_gold"}
# The silver tables as Spark creates them (transforms.py), in Trino's type names.
SILVER_DDL = [
    """CREATE TABLE test_silver.measurements (
        location_id bigint, sensors_id bigint, location_name varchar, parameter varchar,
        units varchar, value double, measured_at timestamp(6) with time zone, local_date date,
        lat double, lon double, ingested_at timestamp(6) with time zone)""",
    """CREATE TABLE test_silver.fire_hotspots (
        latitude double, longitude double, detected_at timestamp(6) with time zone,
        local_date date, satellite varchar, confidence varchar, frp double, daynight varchar,
        source varchar, ingested_at timestamp(6) with time zone)""",
]
TABLES = [
    "test_silver.measurements",
    "test_silver.fire_hotspots",
    "test_silver.locations",
    "test_gold.dim_station",
    "test_gold.fct_station_day",
]
BANGKOK = (1, 13.75, 100.50)
CHIANG_MAI = (2, 18.79, 98.98)
LOADED = "2026-10-01 00:00:00 UTC"


@pytest.fixture(scope="module")
def conn():
    try:
        conn = local_trino.connect()
        local_trino.query(conn, "SELECT 1")
    except (trino.exceptions.TrinoError, OSError) as error:
        if os.environ.get("REQUIRE_TRINO"):
            raise
        pytest.skip(f"Trino is not running (make local-up): {error}")
    return conn


@pytest.fixture
def db(conn):
    """Empty silver and gold tables, created from sql/ddl/ as the pipeline does."""
    local_trino.query(conn, "DROP VIEW IF EXISTS test_gold.v_station_day")
    for table in TABLES:
        local_trino.query(conn, f"DROP TABLE IF EXISTS {table}")
    local_trino.create_tables(conn, SCHEMAS)
    for ddl in SILVER_DDL:
        local_trino.query(conn, ddl)
    return conn


def sql(conn, statement):
    return local_trino.query(conn, statement)


def insert(conn, table, rows):
    sql(conn, f"INSERT INTO {table} VALUES {', '.join(rows)}")


def readings(station, day, values, sensor=None, loaded=LOADED, parameter="pm25"):
    """One PM2.5 reading per value, hourly from 01:00 local time, like the OpenAQ archive."""
    location_id, lat, lon = station
    start = dt.datetime.fromisoformat(f"{day}T00:00:00+07:00")
    return [
        f"({location_id}, {sensor or location_id * 10}, 'station {location_id}', '{parameter}', "
        f"'µg/m³', {value}, TIMESTAMP '{start + dt.timedelta(hours=h + 1)}', DATE '{day}', "
        f"{lat}, {lon}, TIMESTAMP '{loaded}')"
        for h, value in enumerate(values)
    ]


def fire(day, lat, lon, confidence="n", loaded=LOADED):
    return (
        f"({lat}, {lon}, TIMESTAMP '{day} 06:30:00 UTC', DATE '{day}', 'N20', '{confidence}', "
        f"3.2, 'D', 'VIIRS_NOAA20_NRT', TIMESTAMP '{loaded}')"
    )


def location(location_id, name):
    return f"({location_id}, '{name}', 'Thailand', 13.0, 100.0, 'Air4Thai')"


def build(conn):
    local_trino.run_file(conn, "gold/dim_station.sql", SCHEMAS)
    local_trino.run_file(conn, "gold/fct_station_day.sql", SCHEMAS)


def fact_rows(conn):
    return sql(conn, "SELECT * FROM test_gold.fct_station_day ORDER BY location_id, local_date")


def test_daily_average_hours_and_fires_within_100_km(db):
    day = "2026-03-10"
    insert(
        db,
        "test_silver.measurements",
        # Two sensors at one station report the same 24 hours: 24 hours, not 48.
        readings(BANGKOK, day, [40] * 24, sensor=11)
        + readings(BANGKOK, day, [50] * 24, sensor=12)
        + readings(BANGKOK, day, [300] * 24, sensor=13, parameter="pm10")
        + readings(CHIANG_MAI, day, [10] * 12),
    )
    insert(
        db,
        "test_silver.fire_hotspots",
        [
            fire(day, 13.80, 100.60, "h"),  # about 12 km from Bangkok
            fire("2026-03-09", 13.80, 100.60, "h"),  # the day before
            fire(day, 18.90, 99.00, "n"),  # about 12 km from Chiang Mai
            fire(day, 18.80, 99.10, "l"),  # low confidence
            fire(day, 19.90, 99.00, "h"),  # about 123 km away
        ],
    )
    insert(db, "test_silver.locations", [location(1, "Bangkok"), location(2, "Chiang Mai")])

    build(db)

    rows = sql(
        db,
        """SELECT station_name, pm25_avg, pm25_max, hours, fires_100km, complete_day,
                  over_thai_std, over_who
           FROM test_gold.v_station_day ORDER BY location_id""",
    )
    assert rows == [
        ["Bangkok", 45.0, 50.0, 24, 1, True, True, True],
        ["Chiang Mai", 10.0, 10.0, 12, 1, False, False, False],
    ]


def test_rebuilds_only_days_with_silver_rows_newer_than_the_watermark(db):
    insert(
        db,
        "test_silver.measurements",
        readings(BANGKOK, "2026-03-10", [20] * 23) + readings(BANGKOK, "2026-03-11", [30] * 24),
    )
    build(db)
    first = fact_rows(db)

    build(db)
    assert fact_rows(db) == first

    # A late reading loaded into silver before the last gold build ran, but after the silver
    # load that build used. A watermark based on the build time would skip it.
    late = "2026-10-02 00:00:00 UTC"
    late_reading = readings(BANGKOK, "2026-03-10", [20] * 23 + [44], loaded=late)[-1]
    insert(db, "test_silver.measurements", [late_reading])
    build(db)

    day_10, day_11 = fact_rows(db)
    assert day_10[2:6] == [21.0, 44.0, 24, 0]
    assert day_10[6] == dt.datetime(2026, 10, 2)
    assert day_11 == first[1]


def test_stations_that_stop_reporting_keep_their_names(db):
    insert(db, "test_silver.locations", [location(1, "Old name"), location(2, "Stops reporting")])
    build(db)
    sql(db, "DELETE FROM test_silver.locations")
    insert(db, "test_silver.locations", [location(1, "New name")])
    build(db)

    names = sql(db, "SELECT location_id, station_name FROM test_gold.dim_station ORDER BY 1")
    assert names == [[1, "New name"], [2, "Stops reporting"]]


def station_days(days_ago, stations, hours=24):
    return [
        f"({s}, current_date - INTERVAL '{d}' DAY, 20.0, 30.0, {hours}, 0, "
        f"TIMESTAMP '2026-10-01 00:00:00')"
        for d in days_ago
        for s in stations
    ]


def checks(conn):
    [[failed, details]] = local_trino.run_file(conn, "gold/checks.sql", SCHEMAS)
    return failed, details


def test_checks_pass_on_fresh_complete_data(db):
    insert(db, "test_gold.fct_station_day", station_days(range(4, 40), range(1, 11)))

    assert checks(db) == (0, "none")


def test_checks_report_each_problem(db):
    rows = station_days(range(11, 41), range(1, 11))
    rows += station_days([10], [1, 2])  # newest day is 10 days old and has 2 stations
    rows += station_days([20], [1])  # a second row for station 1 on that day
    rows += station_days([25], [99], hours=25)
    insert(db, "test_gold.fct_station_day", rows)

    failed, details = checks(db)

    newest = dt.datetime.now(dt.UTC).date() - dt.timedelta(days=10)
    assert failed == 4
    assert details.split("; ") == [
        "duplicate (location_id, local_date) keys: 1",
        f"newest day: {newest}",
        "rows with impossible values: 1",
        f"stations on {newest}: 2, 30-day median 10",
    ]


def test_an_empty_gold_table_fails_the_freshness_check(db):
    assert checks(db) == (1, "newest day: none")
