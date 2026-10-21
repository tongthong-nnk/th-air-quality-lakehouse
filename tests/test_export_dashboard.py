import json

import pytest

import export_dashboard


def station(location_id, name):
    return {
        "location_id": location_id,
        "station_name": name,
        "locality": None,
        "latitude": 13.7,
        "longitude": 100.5,
    }


def day(location_id, local_date, pm25, fires=0):
    return {
        "location_id": location_id,
        "local_date": local_date,
        "pm25_avg": pm25,
        "fires_100km": fires,
        "over_thai_std": pm25 > 37.5,
    }


STATIONS = [station(1, "Bangkok"), station(2, "Chiang Mai")]
DAYS = [
    day(1, "2026-02-27", 20.0),
    day(1, "2026-03-01", 40.0, fires=2),
    day(2, "2026-02-28", 60.0, fires=9),
]


def test_series_share_one_date_range_with_nulls_for_missing_days():
    data = export_dashboard.build(STATIONS, DAYS)

    assert (data["first_day"], data["last_day"]) == ("2026-02-27", "2026-03-01")
    bangkok, chiang_mai = data["stations"]
    assert bangkok["pm25"] == [20.0, None, 40.0]
    assert bangkok["fires"] == [0, None, 2]
    assert chiang_mai["pm25"] == [None, 60.0, None]


def test_monthly_summary_across_stations():
    months = export_dashboard.build(STATIONS, DAYS)["months"]

    assert months == [
        {
            "month": "2026-02",
            "stations": 2,
            "pm25_avg": 40.0,
            "over_thai_pct": 50.0,
            "fires_avg": 4.5,
        },
        {
            "month": "2026-03",
            "stations": 1,
            "pm25_avg": 40.0,
            "over_thai_pct": 100.0,
            "fires_avg": 2.0,
        },
    ]


def test_without_fire_data_the_fire_fields_are_left_out():
    data = export_dashboard.build(STATIONS, DAYS, fires=False)

    assert data["has_fires"] is False
    assert "fires" not in data["stations"][0]
    assert "fires_avg" not in data["months"][0]


def test_an_empty_view_is_an_error():
    with pytest.raises(RuntimeError, match="no complete station-days"):
        export_dashboard.build(STATIONS, [])


class FakeAthena:
    """Answers like boto3's Athena client: values as strings, column names in the first row."""

    def __init__(self, states, pages=()):
        self.states = iter(states)
        self.pages = pages
        self.started = None

    def start_query_execution(self, QueryString, WorkGroup):
        self.started = (QueryString, WorkGroup)
        return {"QueryExecutionId": "q-1"}

    def get_query_execution(self, QueryExecutionId):
        state, reason = next(self.states)
        return {"QueryExecution": {"Status": {"State": state, "StateChangeReason": reason}}}

    def get_paginator(self, name):
        assert name == "get_query_results"
        return self

    def paginate(self, QueryExecutionId):
        return iter(self.pages)


def page(rows, header=False):
    columns = [
        {"Name": "location_id", "Type": "bigint"},
        {"Name": "pm25_avg", "Type": "double"},
        {"Name": "over_thai_std", "Type": "boolean"},
        {"Name": "local_date", "Type": "varchar"},
    ]
    if header:
        rows = [[c["Name"] for c in columns], *rows]
    cells = [{"Data": [{} if v is None else {"VarCharValue": v} for v in r]} for r in rows]
    return {"ResultSet": {"ResultSetMetadata": {"ColumnInfo": columns}, "Rows": cells}}


def test_athena_rows_are_read_from_every_page_and_typed():
    client = FakeAthena(
        states=[("RUNNING", ""), ("SUCCEEDED", "")],
        pages=[
            page([["1", "41.5", "true", "2026-03-01"]], header=True),
            page([["2", None, "false", "2026-03-02"]]),
        ],
    )

    rows = export_dashboard.athena_query("thaq", client, poll_seconds=0)("SELECT 1")

    assert client.started == ("SELECT 1", "thaq")
    assert rows == [
        {"location_id": 1, "pm25_avg": 41.5, "over_thai_std": True, "local_date": "2026-03-01"},
        {"location_id": 2, "pm25_avg": None, "over_thai_std": False, "local_date": "2026-03-02"},
    ]


def test_a_failed_athena_query_raises_with_its_reason():
    client = FakeAthena(states=[("FAILED", "TABLE_NOT_FOUND: v_station_day")])

    with pytest.raises(RuntimeError, match="q-1 FAILED: TABLE_NOT_FOUND"):
        export_dashboard.athena_query("thaq", client, poll_seconds=0)("SELECT 1")


def test_export_writes_compact_json(tmp_path):
    answers = {export_dashboard.STATIONS_SQL: STATIONS, export_dashboard.DAYS_SQL: DAYS}
    path = tmp_path / "data" / "dashboard.json"

    export_dashboard.export(answers.__getitem__, path)

    assert json.loads(path.read_text())["stations"][1]["name"] == "Chiang Mai"
    assert ": " not in path.read_text()
