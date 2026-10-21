"""Run the Athena SQL in sql/ on the local Trino from compose.yaml.

Athena v3 is built on Trino, so the MERGE, view and check statements run unchanged. Only the
CREATE TABLE statements differ: Athena uses Hive syntax for Iceberg tables (LOCATION,
TBLPROPERTIES, `string`), which to_trino() rewrites.
"""

import pathlib
import re

import trino

SQL_DIR = pathlib.Path(__file__).resolve().parent.parent / "sql"


def connect(host: str = "localhost", port: int = 8080) -> trino.dbapi.Connection:
    return trino.dbapi.connect(
        host=host, port=port, user="local", catalog="iceberg", timezone="UTC"
    )


def query(conn: trino.dbapi.Connection, sql: str) -> list[list]:
    cursor = conn.cursor()
    cursor.execute(sql)
    return cursor.fetchall()


def records(conn: trino.dbapi.Connection, sql: str) -> list[dict]:
    cursor = conn.cursor()
    cursor.execute(sql)
    rows = cursor.fetchall()
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in rows]


def literal(value) -> str:
    """A Python value as a SQL literal, for the few rows the local run inserts."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return str(value).upper()
    if isinstance(value, int | float):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def to_trino(ddl: str) -> str:
    """Athena CREATE [EXTERNAL] TABLE to a Trino Iceberg CREATE TABLE. Views pass through.

    The external JSON table over raw/ becomes an ordinary Iceberg table that the caller fills.
    """
    ddl = re.split(r"\n(?:ROW FORMAT|LOCATION|TBLPROPERTIES)\b", ddl)[0]
    ddl = ddl.replace("CREATE EXTERNAL TABLE", "CREATE TABLE")
    ddl = re.sub(r"\bstring\b", "varchar", ddl)
    return re.sub(r"\btimestamp\b(?!\s*\()", "timestamp(6)", ddl)


def read_sql(name: str, schemas: dict[str, str] | None = None) -> str:
    """A file from sql/. `schemas` renames aq_silver and aq_gold, so tests keep to their own."""
    text = (SQL_DIR / name).read_text()
    for old, new in (schemas or {}).items():
        text = text.replace(f"{old}.", f"{new}.")
    return text


def create_tables(conn: trino.dbapi.Connection, schemas: dict[str, str] | None = None) -> None:
    """Run sql/ddl/ in file order, as the pipeline does before building gold."""
    for schema in ("aq_silver", "aq_gold"):
        query(conn, f"CREATE SCHEMA IF NOT EXISTS {(schemas or {}).get(schema, schema)}")
    for path in sorted((SQL_DIR / "ddl").glob("*.sql")):
        query(conn, to_trino(read_sql(f"ddl/{path.name}", schemas)))


def run_file(
    conn: trino.dbapi.Connection, name: str, schemas: dict[str, str] | None = None
) -> list[list]:
    return query(conn, read_sql(name, schemas))
