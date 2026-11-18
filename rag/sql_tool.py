"""query_sql tool: read-only SQL over the gold tables (DuckDB), for counts and aggregates.

Defence in depth, the LLM-written SQL is never trusted:
  1. a single SELECT / WITH statement (DuckDB's own parser, json_serialize_sql, rejects anything else);
  2. every table referenced must be in the gold allowlist (CTE names excepted);
  3. the connection is read_only with external access disabled (no files, no network, no ATTACH);
  4. results are capped (LIMIT) and the query is interrupted after a timeout.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from typing import Any

import duckdb

ALLOWED_TABLES = {
    "fct_recalls",
    "fct_complaints",
    "dim_vehicle",
    "dim_component",
    "bridge_complaint_component",
}
ALLOWED_SCHEMAS = {"gold", "lakehouse", ""}  # "" = unqualified, resolved through search_path=gold

SCHEMA_DOC = """\
gold.fct_recalls (one row per recall campaign x vehicle):
  campaign_number, make, model, model_year, component, component_group, category, category_fr,
  report_received_date (DATE), manufacturer, summary, consequence, remedy, park_it (BOOL),
  park_outside (BOOL), over_the_air_update (BOOL)
gold.fct_complaints (one row per complaint x vehicle; count DISTINCT odi_number; NO category column,
  join bridge_complaint_component USING (odi_number) to filter complaints by category):
  odi_number, make, model, model_year, complaint_filed_date (DATE), incident_date (DATE),
  crash (BOOL), fire (BOOL), number_of_injuries, number_of_deaths, components_raw, summary
gold.bridge_complaint_component (complaint x component group):
  odi_number, vehicle_key, component_group, category, category_fr
gold.dim_vehicle: vehicle_key, make, model, model_year
gold.dim_component: component_key, component_group, category, category_fr
Values are UPPERCASE: make='VOLKSWAGEN'; models: GOLF, GOLF GTI, GOLF R, GOLF SPORTWAGEN, E-GOLF, JETTA,
JETTA GLI, PASSAT, ARTEON, ATLAS, ATLAS CROSS SPORT, TIGUAN, TIGUAN LIMITED, BEETLE, ALLTRACK.
Filter broad topics with `category` (not component_group): BRAKES, ENGINE, POWERTRAIN, FUEL_SYSTEM, ELECTRICAL,
AIRBAGS, SEAT_BELTS, SEATS, CHILD_SEAT, STEERING, SUSPENSION, TIRES_WHEELS, LIGHTING, VISIBILITY, ADAS,
SPEED_CONTROL, STRUCTURE, LATCHES_LOCKS, EQUIPMENT, OTHER (category_fr = French label)."""


class SqlRejected(ValueError):
    """The query is not allowed (not a SELECT, forbidden table...)."""


@dataclass
class SqlResult:
    sql: str
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    truncated: bool = False
    error: str | None = None

    def as_text(self, max_chars: int = 4000) -> str:
        if self.error:
            return f"SQL ERROR: {self.error}"
        if not self.rows:
            return "No rows."
        lines = [" | ".join(self.columns)] + [" | ".join("" if v is None else str(v) for v in r) for r in self.rows]
        text = "\n".join(lines)
        if self.truncated:
            text += "\n(truncated)"
        return text[:max_chars]


def _strip(sql: str) -> str:
    sql = re.sub(r"--[^\n]*", "", sql).strip()
    return sql.rstrip(";").strip()


def _walk(node: Any):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def referenced_tables(conn: duckdb.DuckDBPyConnection, sql: str) -> set[tuple[str, str]]:
    """(schema, table) of every base table in the statement, from DuckDB's parser.
    Raises SqlRejected for anything that is not a single SELECT."""
    if ";" in sql:
        raise SqlRejected("only one statement is allowed")
    tree = json.loads(conn.execute("SELECT json_serialize_sql(?::VARCHAR)", [sql]).fetchone()[0])
    if tree.get("error"):
        raise SqlRejected(f"only SELECT queries are allowed ({tree.get('error_message', 'parse error')})")
    if len(tree.get("statements") or []) != 1:
        raise SqlRejected("only one statement is allowed")
    ctes = {
        entry["key"].lower()
        for node in _walk(tree)
        for entry in ((node.get("cte_map") or {}).get("map") or [])
        if isinstance(entry, dict) and "key" in entry
    }
    tables = set()
    for node in _walk(tree):
        if node.get("type") == "BASE_TABLE":
            schema, table = (node.get("schema_name") or "").lower(), node["table_name"].lower()
            if not schema and table in ctes:
                continue
            if (node.get("catalog_name") or "").lower() not in {"", "lakehouse"}:
                raise SqlRejected("other catalogs are not allowed")
            tables.add((schema, table))
        if node.get("type") == "TABLE_FUNCTION":
            raise SqlRejected("table functions are not allowed")
    return tables


def check_sql(conn: duckdb.DuckDBPyConnection, sql: str) -> str:
    sql = _strip(sql)
    if not sql:
        raise SqlRejected("empty query")
    for schema, table in referenced_tables(conn, sql):
        if schema not in ALLOWED_SCHEMAS or table not in ALLOWED_TABLES:
            name = f"{schema}.{table}" if schema else table
            raise SqlRejected(f"table {name} is not allowed; use: {', '.join(sorted(ALLOWED_TABLES))}")
    return sql


def connect_read_only(path: str) -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(path, read_only=True, config={"enable_external_access": False})
    conn.execute("SET search_path = 'gold'")
    return conn


def run_sql(sql: str, path: str, max_rows: int = 50, timeout_s: float = 10.0) -> SqlResult:
    """Validate then execute; never raises (errors are returned to the agent so it can fix its query)."""
    try:
        conn = connect_read_only(path)
    except duckdb.Error as exc:  # e.g. file locked by a running dbt build
        return SqlResult(sql=sql, error=f"database unavailable: {exc}")
    timer = threading.Timer(timeout_s, conn.interrupt)
    try:
        checked = check_sql(conn, sql)
        timer.start()
        cur = conn.execute(f"SELECT * FROM ({checked}) AS q LIMIT {max_rows + 1}")
        columns = [d[0] for d in cur.description]
        rows = [list(r) for r in cur.fetchall()]
        return SqlResult(
            sql=checked,
            columns=columns,
            rows=[[_jsonable(v) for v in r] for r in rows[:max_rows]],
            truncated=len(rows) > max_rows,
        )
    except SqlRejected as exc:
        return SqlResult(sql=sql, error=f"rejected: {exc}")
    except duckdb.InterruptException:
        return SqlResult(sql=sql, error=f"timeout after {timeout_s}s")
    except duckdb.Error as exc:
        return SqlResult(sql=sql, error=str(exc).splitlines()[0])
    finally:
        timer.cancel()
        conn.close()


def _jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    return str(v)
