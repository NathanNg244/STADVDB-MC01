import argparse
import datetime as dt
import json
import os
import threading
import time
import webbrowser
from decimal import Decimal
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from sqlalchemy import URL, create_engine, text

DB_HOST = os.getenv("MYSQL_HOST", "localhost")
DB_PORT = int(os.getenv("MYSQL_PORT", "3306"))
DB_USER = os.getenv("MYSQL_USER", "root")
DB_PASSWORD = os.getenv("MYSQL_PASSWORD", "12345678")
DB_NAME = os.getenv("MYSQL_DATABASE", "financial_dw")

DATABASE_URL = os.getenv(
    "OLAP_DATABASE_URL",
    URL.create(
        "mysql+pymysql",
        username=DB_USER,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME,
    ),
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=3600)

DIMENSIONS = {
    "date": {
        "label": "Date",
        "table": "dimdate",
        "alias": "d",
        "join": "JOIN dimdate d ON f.date_key = d.date_key",
        "levels": {
            "year": {
                "label": "Year",
                "expr": "d.year_number",
                "type": "int",
            },
            "quarter": {
                "label": "Quarter",
                "expr": "CONCAT(d.year_number, '-Q', d.quarter_number)",
                "type": "str",
            },
            "month": {
                "label": "Month",
                "expr": "CONCAT(d.year_number, '-', LPAD(d.month_number, 2, '0'))",
                "type": "str",
            },
            "date": {
                "label": "Date",
                "expr": "d.full_date",
                "type": "date",
            },
        },
    },
    "district": {
        "label": "District",
        "table": "dimdistrict",
        "alias": "ds",
        "join": "JOIN dimdistrict ds ON f.district_key = ds.district_key",
        "levels": {
            "region": {
                "label": "Region",
                "expr": "ds.region",
                "type": "str",
            },
            "district": {
                "label": "District",
                "expr": "ds.district_name",
                "type": "str",
            },
        },
    },
    "transaction_type": {
        "label": "Transaction type",
        "table": "dimtransactiontype",
        "alias": "tt",
        "join": "JOIN dimtransactiontype tt ON f.transaction_type_key = tt.transaction_type_key",
        "levels": {
            "type": {
                "label": "Type",
                "expr": "tt.type",
                "type": "str",
            },
            "operation": {
                "label": "Operation",
                "expr": "tt.operation",
                "type": "str",
            },
            "k_symbol": {
                "label": "K-symbol",
                "expr": "tt.k_symbol",
                "type": "str",
            },
            "bank": {
                "label": "Bank",
                "expr": "tt.bank",
                "type": "str",
            },
        },
    },
    "account": {
        "label": "Account",
        "table": "dimaccount",
        "alias": "a",
        "join": "JOIN dimaccount a ON f.account_key = a.account_key",
        "levels": {
            "frequency": {
                "label": "Statement frequency",
                "expr": "a.frequency",
                "type": "str",
            },
        },
    },
}

MEASURES = {
    "total_amount": {
        "label": "Total amount",
        "expr": "COALESCE(SUM(f.amount), 0)",
        "format": "number",
        "additive": True,
    },
    "transaction_count": {
        "label": "Transaction count",
        "expr": "COUNT(*)",
        "format": "integer",
        "additive": True,
    },
    "avg_amount": {
        "label": "Average amount",
        "expr": "AVG(f.amount)",
        "format": "number",
        "additive": False,
    },
    "credit_amount": {
        "label": "Credit amount",
        "expr": "COALESCE(SUM(CASE WHEN f.amount > 0 THEN f.amount ELSE 0 END), 0)",
        "format": "number",
        "additive": True,
    },
    "debit_amount": {
        "label": "Debit amount",
        "expr": "COALESCE(SUM(CASE WHEN f.amount < 0 THEN -f.amount ELSE 0 END), 0)",
        "format": "number",
        "additive": True,
    },
    "balance_sum": {
        "label": "Balance sum",
        "expr": "COALESCE(SUM(f.balance), 0)",
        "format": "number",
        "additive": True,
    },
    "largest_amount": {
        "label": "Largest amount",
        "expr": "MAX(f.amount)",
        "format": "number",
        "additive": False,
    },
    "smallest_amount": {
        "label": "Smallest amount",
        "expr": "MIN(f.amount)",
        "format": "number",
        "additive": False,
    },
}

FILTER_KEYS = [
    "date.year",
    "district.region",
    "district.district",
    "transaction_type.type",
    "transaction_type.operation",
    "transaction_type.k_symbol",
    "transaction_type.bank",
    "account.frequency",
]

SORT_OPTIONS = {"metric_desc", "metric_asc", "row_asc", "row_desc"}
MAX_FLAT_ROWS = 5000
MAX_PIVOT_CELLS = 20000


def json_value(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return value


def parse_date(value, label):
    if value in (None, ""):
        return None
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"{label} must use YYYY-MM-DD format.") from exc


def parse_number(value, label):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a number.") from exc


def normalize_level(value):
    if not value:
        return None
    if not isinstance(value, dict):
        raise ValueError("A dimension level must be an object.")
    dimension_key = value.get("dimension")
    level_key = value.get("level")
    if dimension_key not in DIMENSIONS:
        raise ValueError("Unknown dimension.")
    levels = DIMENSIONS[dimension_key]["levels"]
    if level_key not in levels:
        raise ValueError("Unknown dimension level.")
    level = levels[level_key]
    return {
        "dimension": dimension_key,
        "dimension_label": DIMENSIONS[dimension_key]["label"],
        "level": level_key,
        "label": level["label"],
        "expr": level["expr"],
        "type": level["type"],
    }


def coerce_filter_value(value, value_type):
    if value_type == "int":
        return int(value)
    if value_type == "date":
        return dt.date.fromisoformat(str(value))
    return str(value)


def build_spec(config):
    if not isinstance(config, dict):
        raise ValueError("Query payload must be an object.")

    measure_key = config.get("measure") or "total_amount"
    if measure_key not in MEASURES:
        raise ValueError("Unknown measure.")
    measure = {"key": measure_key, **MEASURES[measure_key]}

    row = normalize_level(config.get("row"))
    column = normalize_level(config.get("column"))
    if column and not row:
        row, column = column, None
    if row and column and row["dimension"] == column["dimension"] and row["level"] == column["level"]:
        column = None

    joins = []
    joined = set()

    def require_dimension(dimension_key):
        if dimension_key not in joined:
            joins.append(DIMENSIONS[dimension_key]["join"])
            joined.add(dimension_key)

    if row:
        require_dimension(row["dimension"])
    if column:
        require_dimension(column["dimension"])

    where = []
    params = {}
    parameter_index = 0
    filters = config.get("filters") or {}
    if not isinstance(filters, dict):
        raise ValueError("Filters must be an object.")

    for filter_key, values in filters.items():
        if filter_key not in FILTER_KEYS:
            continue
        if not isinstance(values, list):
            values = [values]
        values = [value for value in values if value not in (None, "")]
        if not values:
            continue
        dimension_key, level_key = filter_key.split(".", 1)
        level = DIMENSIONS[dimension_key]["levels"][level_key]
        require_dimension(dimension_key)
        placeholders = []
        for value in values:
            parameter_name = f"filter_{parameter_index}"
            parameter_index += 1
            placeholders.append(f":{parameter_name}")
            params[parameter_name] = coerce_filter_value(value, level["type"])
        where.append(f"{level['expr']} IN ({', '.join(placeholders)})")

    date_from = parse_date(config.get("date_from"), "Start date")
    date_to = parse_date(config.get("date_to"), "End date")
    if date_from or date_to:
        require_dimension("date")
        if date_from:
            where.append("d.full_date >= :date_from")
            params["date_from"] = date_from
        if date_to:
            where.append("d.full_date <= :date_to")
            params["date_to"] = date_to
    if date_from and date_to and date_from > date_to:
        raise ValueError("Start date cannot be after end date.")

    amount_min = parse_number(config.get("amount_min"), "Minimum amount")
    amount_max = parse_number(config.get("amount_max"), "Maximum amount")
    if amount_min is not None:
        where.append("f.amount >= :amount_min")
        params["amount_min"] = amount_min
    if amount_max is not None:
        where.append("f.amount <= :amount_max")
        params["amount_max"] = amount_max
    if amount_min is not None and amount_max is not None and amount_min > amount_max:
        raise ValueError("Minimum amount cannot be greater than maximum amount.")

    sort = config.get("sort") or "metric_desc"
    if sort not in SORT_OPTIONS:
        sort = "metric_desc"

    try:
        limit = int(config.get("limit") or 200)
    except (TypeError, ValueError) as exc:
        raise ValueError("Row limit must be an integer.") from exc
    limit = max(1, min(limit, MAX_FLAT_ROWS))

    return {
        "row": row,
        "column": column,
        "measure": measure,
        "joins": joins,
        "where": where,
        "params": params,
        "sort": sort,
        "limit": limit,
    }


def execute_spec(spec):
    measure = spec["measure"]
    select_parts = []
    group_parts = []

    if spec["row"]:
        select_parts.append(f"{spec['row']['expr']} AS row_value")
        group_parts.append(spec["row"]["expr"])
    if spec["column"]:
        select_parts.append(f"{spec['column']['expr']} AS col_value")
        group_parts.append(spec["column"]["expr"])
    select_parts.append(f"{measure['expr']} AS metric")

    from_clause = "FROM facttransaction f " + " ".join(spec["joins"])
    where_clause = ""
    if spec["where"]:
        where_clause = " WHERE " + " AND ".join(spec["where"])

    sql = "SELECT " + ", ".join(select_parts) + " " + from_clause + where_clause

    if group_parts:
        sql += " GROUP BY " + ", ".join(group_parts)

    params = dict(spec["params"])
    is_pivot = bool(spec["row"] and spec["column"])
    truncated = False

    if is_pivot:
        sql += " ORDER BY row_value ASC, col_value ASC LIMIT :row_limit"
        params["row_limit"] = MAX_PIVOT_CELLS + 1
    elif spec["row"]:
        if spec["sort"] == "row_asc":
            order_by = "row_value ASC"
        elif spec["sort"] == "row_desc":
            order_by = "row_value DESC"
        elif spec["sort"] == "metric_asc":
            order_by = "metric ASC"
        else:
            order_by = "metric DESC"
        sql += f" ORDER BY {order_by} LIMIT :row_limit"
        params["row_limit"] = spec["limit"] + 1

    summary_sql = f"SELECT {measure['expr']} AS value {from_clause}{where_clause}"

    with engine.connect() as connection:
        summary_value = connection.execute(text(summary_sql), spec["params"]).scalar()
        records = connection.execute(text(sql), params).mappings().all()

    if is_pivot and len(records) > MAX_PIVOT_CELLS:
        records = records[:MAX_PIVOT_CELLS]
        truncated = True
    elif not is_pivot and spec["row"] and len(records) > spec["limit"]:
        records = records[: spec["limit"]]
        truncated = True

    result = {
        "measure": measure,
        "row": spec["row"],
        "column": spec["column"],
        "summary": {"value": json_value(summary_value)},
        "pivot": None,
        "rows": [],
        "meta": {
            "truncated": truncated,
            "group_count": 0,
            "sql": sql,
        },
    }

    if is_pivot:
        column_keys = []
        seen_columns = set()
        row_values = {}
        for record in records:
            row_value = json_value(record["row_value"])
            column_value = json_value(record["col_value"])
            column_key = str(column_value)
            if column_key not in seen_columns:
                seen_columns.add(column_key)
                column_keys.append({"key": column_key, "label": str(column_value)})
            row_values.setdefault(row_value, {})[column_key] = json_value(record["metric"])

        pivot_rows = []
        for row_value, values in row_values.items():
            total = None
            if measure["additive"]:
                total = sum(value for value in values.values() if value is not None)
            pivot_rows.append(
                {
                    "label": str(row_value),
                    "values": values,
                    "total": total,
                }
            )

        column_totals = {}
        grand_total = None
        if measure["additive"]:
            for column in column_keys:
                column_totals[column["key"]] = sum(
                    row["values"].get(column["key"]) or 0 for row in pivot_rows
                )
            grand_total = sum(column_totals.values())

        result["pivot"] = {
            "row_label": spec["row"]["label"],
            "column_label": spec["column"]["label"],
            "columns": column_keys,
            "rows": pivot_rows,
            "column_totals": column_totals,
            "grand_total": grand_total,
        }
        result["meta"]["group_count"] = len(pivot_rows)
        result["meta"]["column_count"] = len(column_keys)
    else:
        for record in records:
            result["rows"].append(
                {
                    "row": json_value(record["row_value"]) if spec["row"] else "All transactions",
                    "metric": json_value(record["metric"]),
                }
            )
        result["meta"]["group_count"] = len(result["rows"])

    return result


def run_query(config):
    started = time.perf_counter()
    result = execute_spec(build_spec(config))
    result["meta"]["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return result


def get_metadata():
    with engine.connect() as connection:
        fact_rows = connection.execute(
            text("SELECT COUNT(*) FROM facttransaction")
        ).scalar_one()
        date_range = connection.execute(
            text("SELECT MIN(full_date), MAX(full_date) FROM dimdate")
        ).one()

        filter_options = {}
        for filter_key in FILTER_KEYS:
            dimension_key, level_key = filter_key.split(".", 1)
            dimension = DIMENSIONS[dimension_key]
            level = dimension["levels"][level_key]
            expr = level["expr"]
            sql = (
                f"SELECT DISTINCT {expr} AS value "
                f"FROM {dimension['table']} {dimension['alias']} "
                f"WHERE {expr} IS NOT NULL ORDER BY {expr}"
            )
            values = [json_value(record[0]) for record in connection.execute(text(sql))]
            filter_options[filter_key] = {
                "label": f"{dimension['label']} - {level['label']}",
                "dimension": dimension_key,
                "level": level_key,
                "values": values,
            }

    dimensions = {}
    for dimension_key, dimension in DIMENSIONS.items():
        dimensions[dimension_key] = {
            "label": dimension["label"],
            "levels": [
                {"key": level_key, "label": level["label"]}
                for level_key, level in dimension["levels"].items()
            ],
        }

    measures = [
        {
            "key": measure_key,
            "label": measure["label"],
            "format": measure["format"],
            "additive": measure["additive"],
        }
        for measure_key, measure in MEASURES.items()
    ]

    return {
        "database": DB_NAME,
        "fact_rows": int(fact_rows),
        "date_range": {
            "min": json_value(date_range[0]),
            "max": json_value(date_range[1]),
        },
        "dimensions": dimensions,
        "measures": measures,
        "filter_options": filter_options,
    }


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Financial OLAP Explorer</title>
<style>
:root {
  --bg: #f4f7fb;
  --panel: #ffffff;
  --ink: #172033;
  --muted: #667085;
  --line: #dce3ee;
  --accent: #2563eb;
  --accent-dark: #1d4ed8;
  --accent-soft: #eaf1ff;
  --good: #0f9f6e;
  --bad: #d92d20;
  --shadow: 0 12px 34px rgba(25, 42, 70, 0.08);
}
* { box-sizing: border-box; }
body {
  margin: 0;
  min-height: 100vh;
  background: radial-gradient(circle at top left, #e8f0ff 0, transparent 32rem), var(--bg);
  color: var(--ink);
  font-family: "Segoe UI", system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
}
button, input, select { font: inherit; }
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  padding: 22px 28px;
  border-bottom: 1px solid var(--line);
  background: rgba(255, 255, 255, 0.9);
  backdrop-filter: blur(12px);
  position: sticky;
  top: 0;
  z-index: 10;
}
.brand { display: flex; align-items: center; gap: 14px; }
.logo {
  display: grid;
  place-items: center;
  width: 48px;
  height: 48px;
  border-radius: 14px;
  background: linear-gradient(135deg, #2563eb, #7c3aed);
  color: #fff;
  font-size: 12px;
  font-weight: 800;
  letter-spacing: 0.08em;
  box-shadow: 0 8px 20px rgba(37, 99, 235, 0.28);
}
h1, h2, p { margin: 0; }
h1 { font-size: 20px; letter-spacing: -0.02em; }
h2 { font-size: 13px; text-transform: uppercase; letter-spacing: 0.09em; color: var(--muted); }
.brand p, .hint { color: var(--muted); font-size: 12px; margin-top: 4px; }
.header-actions { display: flex; align-items: center; gap: 12px; }
.status {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  font-size: 12px;
  color: var(--muted);
  white-space: nowrap;
}
.dot { width: 8px; height: 8px; border-radius: 50%; background: #98a2b3; }
.dot.online { background: var(--good); box-shadow: 0 0 0 4px rgba(15, 159, 110, 0.12); }
.dot.offline { background: var(--bad); box-shadow: 0 0 0 4px rgba(217, 45, 32, 0.12); }
.layout {
  display: grid;
  grid-template-columns: 330px minmax(0, 1fr);
  gap: 22px;
  padding: 22px 28px 32px;
  max-width: 1680px;
  margin: 0 auto;
}
.sidebar { display: flex; flex-direction: column; gap: 16px; align-self: start; position: sticky; top: 106px; }
.content { display: flex; flex-direction: column; gap: 16px; min-width: 0; }
.panel {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 18px;
  box-shadow: var(--shadow);
  padding: 18px;
}
.sidebar .panel { padding: 16px; }
.sidebar h2 { margin-bottom: 12px; }
label.field { display: flex; flex-direction: column; gap: 6px; font-size: 12px; font-weight: 600; color: #344054; }
select, input[type="date"], input[type="number"] {
  width: 100%;
  min-height: 38px;
  padding: 8px 10px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: #fbfcfe;
  color: var(--ink);
  outline: none;
}
select:focus, input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.12); }
select:disabled { color: #98a2b3; background: #f2f4f7; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.stack { display: flex; flex-direction: column; gap: 12px; }
.preset-grid { display: grid; gap: 8px; }
.preset {
  width: 100%;
  padding: 10px 12px;
  text-align: left;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: #fbfcfe;
  color: #344054;
  cursor: pointer;
  font-size: 12px;
  font-weight: 600;
  transition: 0.15s ease;
}
.preset:hover { border-color: #b8c9ea; background: var(--accent-soft); color: var(--accent-dark); }
.actions { display: grid; grid-template-columns: 1fr auto; gap: 10px; }
.btn {
  border: 0;
  border-radius: 10px;
  min-height: 40px;
  padding: 9px 14px;
  cursor: pointer;
  font-size: 12px;
  font-weight: 700;
  transition: 0.15s ease;
}
.btn.primary { background: var(--accent); color: #fff; box-shadow: 0 8px 18px rgba(37, 99, 235, 0.22); }
.btn.primary:hover { background: var(--accent-dark); }
.btn.ghost { background: #fff; color: #475467; border: 1px solid var(--line); }
.btn.ghost:hover { background: #f8fafc; border-color: #c5d1e4; }
.btn:disabled { opacity: 0.55; cursor: not-allowed; box-shadow: none; }
.filter-group { border-top: 1px solid var(--line); padding-top: 10px; margin-top: 10px; }
.filter-group:first-child { border-top: 0; margin-top: 0; padding-top: 0; }
details summary {
  cursor: pointer;
  list-style: none;
  display: flex;
  justify-content: space-between;
  gap: 10px;
  color: #344054;
  font-size: 12px;
  font-weight: 700;
  padding: 4px 0;
}
details summary::-webkit-details-marker { display: none; }
details summary::after { content: "+"; color: var(--muted); font-weight: 500; }
details[open] summary::after { content: "-"; }
.filter-body { padding-top: 8px; }
.clear-filter {
  border: 0;
  background: transparent;
  color: var(--accent);
  padding: 0 0 7px;
  cursor: pointer;
  font-size: 11px;
  font-weight: 700;
}
.chips { display: flex; flex-wrap: wrap; gap: 6px; max-height: 180px; overflow: auto; padding-right: 2px; }
.chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 8px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: #fbfcfe;
  color: #475467;
  font-size: 11px;
  cursor: pointer;
  user-select: none;
}
.chip input { accent-color: var(--accent); margin: 0; }
.chip:has(input:checked) { border-color: #9ebaf3; background: var(--accent-soft); color: var(--accent-dark); }
.summary { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; }
.card {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 16px;
  box-shadow: var(--shadow);
  padding: 16px 18px;
}
.card span { display: block; color: var(--muted); font-size: 11px; font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase; }
.card strong { display: block; margin-top: 7px; font-size: clamp(20px, 2.4vw, 30px); letter-spacing: -0.03em; }
.result-panel { padding: 20px; }
.result-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; margin-bottom: 16px; }
.result-head h2 { font-size: 18px; text-transform: none; letter-spacing: -0.02em; color: var(--ink); }
.spinner {
  width: 22px;
  height: 22px;
  border: 3px solid #dce7fb;
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
.hidden { display: none; }
@keyframes spin { to { transform: rotate(360deg); } }
.chart { display: flex; flex-direction: column; gap: 8px; margin-bottom: 18px; }
.chart-title { color: var(--muted); font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 2px; }
.bar-row { display: grid; grid-template-columns: minmax(90px, 180px) minmax(80px, 1fr) 110px; gap: 10px; align-items: center; font-size: 12px; }
.bar-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #475467; }
.bar-track { height: 10px; border-radius: 999px; background: #eef3fa; overflow: hidden; }
.bar { height: 100%; min-width: 2px; border-radius: 999px; background: linear-gradient(90deg, #60a5fa, #2563eb); }
.bar.negative { background: linear-gradient(90deg, #fda29b, #d92d20); }
.bar-value { text-align: right; font-variant-numeric: tabular-nums; color: #344054; font-weight: 600; }
.table-wrap { overflow: auto; border: 1px solid var(--line); border-radius: 12px; max-height: 62vh; }
table { width: 100%; border-collapse: collapse; font-size: 12px; }
th, td { padding: 10px 12px; border-bottom: 1px solid #edf1f7; text-align: left; white-space: nowrap; }
th { position: sticky; top: 0; background: #f8fafc; color: #475467; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; z-index: 1; }
tbody tr:hover { background: #f8fbff; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
tr.total-row td { background: #f2f7ff; font-weight: 800; border-top: 2px solid #cfe0ff; }
.empty, .error { padding: 28px; text-align: center; color: var(--muted); font-size: 13px; }
.error { color: var(--bad); background: #fff6f5; border: 1px solid #fecdca; border-radius: 12px; }
.notice { margin-top: 10px; color: #b54708; font-size: 12px; }
@media (max-width: 1050px) {
  .layout { grid-template-columns: 1fr; }
  .sidebar { position: static; }
  .summary { grid-template-columns: 1fr 1fr; }
}
@media (max-width: 680px) {
  .topbar { padding: 16px; align-items: flex-start; }
  .layout { padding: 16px; }
  .summary { grid-template-columns: 1fr; }
  .bar-row { grid-template-columns: 90px minmax(50px, 1fr) 90px; }
  .grid2 { grid-template-columns: 1fr; }
}
</style>
</head>
<body>
<header class="topbar">
  <div class="brand">
    <div class="logo">OLAP</div>
    <div>
      <h1>Financial Data Explorer</h1>
      <p id="connectionText">Connecting to the data warehouse...</p>
    </div>
  </div>
  <div class="header-actions">
    <span class="status"><span id="statusDot" class="dot"></span><span id="statusText">Checking</span></span>
    <button id="exportBtn" class="btn ghost" disabled>Export CSV</button>
  </div>
</header>

<main class="layout">
  <aside class="sidebar">
    <section class="panel">
      <h2>Quick views</h2>
      <div class="preset-grid">
        <button class="preset" data-preset="yearly">Yearly totals</button>
        <button class="preset" data-preset="monthly">Monthly trend</button>
        <button class="preset" data-preset="regions">Regional performance</button>
        <button class="preset" data-preset="types">Transaction mix</button>
        <button class="preset" data-preset="districts">Top districts</button>
      </div>
    </section>

    <section class="panel">
      <h2>Analysis</h2>
      <div class="stack">
        <label class="field">Measure
          <select id="measureSelect"></select>
        </label>
        <div class="grid2">
          <label class="field">Rows
            <select id="rowDimension"></select>
          </label>
          <label class="field">Level
            <select id="rowLevel"></select>
          </label>
        </div>
        <div class="grid2">
          <label class="field">Columns
            <select id="columnDimension"></select>
          </label>
          <label class="field">Level
            <select id="columnLevel"></select>
          </label>
        </div>
        <p class="hint">Leave columns empty for a flat report. Choose both to build a pivot table.</p>
        <div class="grid2">
          <label class="field">Sort
            <select id="sortSelect">
              <option value="metric_desc">Measure: high to low</option>
              <option value="metric_asc">Measure: low to high</option>
              <option value="row_asc">Row: A to Z</option>
              <option value="row_desc">Row: Z to A</option>
            </select>
          </label>
          <label class="field">Row limit
            <select id="limitSelect">
              <option value="50">50</option>
              <option value="100">100</option>
              <option value="200" selected>200</option>
              <option value="500">500</option>
              <option value="1000">1,000</option>
              <option value="5000">5,000</option>
            </select>
          </label>
        </div>
      </div>
    </section>

    <section class="panel">
      <h2>Filters</h2>
      <div class="stack">
        <div class="grid2">
          <label class="field">From
            <input id="dateFrom" type="date">
          </label>
          <label class="field">To
            <input id="dateTo" type="date">
          </label>
        </div>
        <div class="grid2">
          <label class="field">Min amount
            <input id="amountMin" type="number" step="0.01" placeholder="No minimum">
          </label>
          <label class="field">Max amount
            <input id="amountMax" type="number" step="0.01" placeholder="No maximum">
          </label>
        </div>
        <div id="filterOptions"></div>
      </div>
    </section>

    <div class="actions">
      <button id="runBtn" class="btn primary">Run analysis</button>
      <button id="resetBtn" class="btn ghost">Reset</button>
    </div>
  </aside>

  <section class="content">
    <div id="summary" class="summary"></div>
    <section class="panel result-panel">
      <div class="result-head">
        <div>
          <h2 id="resultTitle">Results</h2>
          <p id="queryInfo" class="hint"></p>
        </div>
        <div id="spinner" class="spinner hidden"></div>
      </div>
      <div id="chart" class="chart"></div>
      <div id="tableWrap" class="table-wrap">
        <div class="empty">Loading data warehouse metadata...</div>
      </div>
      <div id="notice"></div>
    </section>
  </section>
</main>

<script>
const state = { metadata: null, lastResult: null };
const elements = {
  connectionText: document.getElementById("connectionText"),
  statusDot: document.getElementById("statusDot"),
  statusText: document.getElementById("statusText"),
  exportBtn: document.getElementById("exportBtn"),
  measureSelect: document.getElementById("measureSelect"),
  rowDimension: document.getElementById("rowDimension"),
  rowLevel: document.getElementById("rowLevel"),
  columnDimension: document.getElementById("columnDimension"),
  columnLevel: document.getElementById("columnLevel"),
  sortSelect: document.getElementById("sortSelect"),
  limitSelect: document.getElementById("limitSelect"),
  dateFrom: document.getElementById("dateFrom"),
  dateTo: document.getElementById("dateTo"),
  amountMin: document.getElementById("amountMin"),
  amountMax: document.getElementById("amountMax"),
  filterOptions: document.getElementById("filterOptions"),
  runBtn: document.getElementById("runBtn"),
  resetBtn: document.getElementById("resetBtn"),
  summary: document.getElementById("summary"),
  resultTitle: document.getElementById("resultTitle"),
  queryInfo: document.getElementById("queryInfo"),
  spinner: document.getElementById("spinner"),
  chart: document.getElementById("chart"),
  tableWrap: document.getElementById("tableWrap"),
  notice: document.getElementById("notice"),
};

const presets = {
  yearly: {
    row: { dimension: "date", level: "year" },
    column: null,
    measure: "total_amount",
    sort: "row_asc",
    limit: "200",
  },
  monthly: {
    row: { dimension: "date", level: "month" },
    column: null,
    measure: "total_amount",
    sort: "row_asc",
    limit: "200",
  },
  regions: {
    row: { dimension: "district", level: "region" },
    column: null,
    measure: "total_amount",
    sort: "metric_desc",
    limit: "200",
  },
  types: {
    row: { dimension: "transaction_type", level: "type" },
    column: null,
    measure: "transaction_count",
    sort: "metric_desc",
    limit: "200",
  },
  districts: {
    row: { dimension: "district", level: "district" },
    column: null,
    measure: "total_amount",
    sort: "metric_desc",
    limit: "20",
  },
};

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatValue(value, format) {
  if (value === null || value === undefined || value === "") return "—";
  if (format === "integer") {
    return new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }).format(value);
  }
  return new Intl.NumberFormat("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value);
}

function setStatus(online, message) {
  elements.statusDot.className = "dot " + (online ? "online" : "offline");
  elements.statusText.textContent = message;
}

function setLoading(loading) {
  elements.spinner.classList.toggle("hidden", !loading);
  elements.runBtn.disabled = loading;
  elements.exportBtn.disabled = loading || !state.lastResult;
}

function fillSelect(select, items, includeNone) {
  select.innerHTML = "";
  if (includeNone) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "None (grand total)";
    select.appendChild(option);
  }
  for (const item of items) {
    const option = document.createElement("option");
    option.value = item.key;
    option.textContent = item.label;
    select.appendChild(option);
  }
}

function updateLevels(dimensionKey, levelSelect, preferredLevel) {
  levelSelect.innerHTML = "";
  if (!dimensionKey || !state.metadata.dimensions[dimensionKey]) {
    levelSelect.disabled = true;
    return;
  }
  const dimension = state.metadata.dimensions[dimensionKey];
  levelSelect.disabled = false;
  for (const level of dimension.levels) {
    const option = document.createElement("option");
    option.value = level.key;
    option.textContent = level.label;
    levelSelect.appendChild(option);
  }
  if (preferredLevel && dimension.levels.some((level) => level.key === preferredLevel)) {
    levelSelect.value = preferredLevel;
  }
}

function setDimensionSelection(which, selection) {
  const dimensionSelect = which === "row" ? elements.rowDimension : elements.columnDimension;
  const levelSelect = which === "row" ? elements.rowLevel : elements.columnLevel;
  dimensionSelect.value = selection ? selection.dimension : "";
  updateLevels(dimensionSelect.value, levelSelect, selection ? selection.level : null);
}

function clearFilters() {
  document.querySelectorAll("input[data-filter-key]").forEach((input) => {
    input.checked = false;
  });
  elements.dateFrom.value = "";
  elements.dateTo.value = "";
  elements.amountMin.value = "";
  elements.amountMax.value = "";
}

function renderFilters() {
  elements.filterOptions.innerHTML = "";
  const openByDefault = new Set([
    "date.year",
    "district.region",
    "transaction_type.type",
    "account.frequency",
  ]);
  for (const [filterKey, option] of Object.entries(state.metadata.filter_options)) {
    const details = document.createElement("details");
    details.className = "filter-group";
    details.open = openByDefault.has(filterKey);

    const summary = document.createElement("summary");
    summary.textContent = option.label + " (" + option.values.length + ")";
    details.appendChild(summary);

    const body = document.createElement("div");
    body.className = "filter-body";

    const clear = document.createElement("button");
    clear.type = "button";
    clear.className = "clear-filter";
    clear.textContent = "Clear selection";
    clear.addEventListener("click", () => {
      details.querySelectorAll("input[type=checkbox]").forEach((input) => {
        input.checked = false;
      });
    });
    body.appendChild(clear);

    const chips = document.createElement("div");
    chips.className = "chips";
    for (const value of option.values) {
      const label = document.createElement("label");
      label.className = "chip";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.value = String(value);
      input.dataset.filterKey = filterKey;
      const text = document.createElement("span");
      text.textContent = String(value);
      label.append(input, text);
      chips.appendChild(label);
    }
    body.appendChild(chips);
    details.appendChild(body);
    elements.filterOptions.appendChild(details);
  }
}

function collectFilters() {
  const filters = {};
  document.querySelectorAll("input[data-filter-key]:checked").forEach((input) => {
    const key = input.dataset.filterKey;
    if (!filters[key]) filters[key] = [];
    filters[key].push(input.value);
  });
  return filters;
}

function buildPayload() {
  const rowDimension = elements.rowDimension.value;
  const columnDimension = elements.columnDimension.value;
  return {
    measure: elements.measureSelect.value,
    row: rowDimension
      ? { dimension: rowDimension, level: elements.rowLevel.value }
      : null,
    column: columnDimension
      ? { dimension: columnDimension, level: elements.columnLevel.value }
      : null,
    sort: elements.sortSelect.value,
    limit: Number(elements.limitSelect.value),
    date_from: elements.dateFrom.value || null,
    date_to: elements.dateTo.value || null,
    amount_min: elements.amountMin.value === "" ? null : Number(elements.amountMin.value),
    amount_max: elements.amountMax.value === "" ? null : Number(elements.amountMax.value),
    filters: collectFilters(),
  };
}

function applyPreset(name) {
  const preset = presets[name];
  if (!preset) return;
  elements.measureSelect.value = preset.measure;
  setDimensionSelection("row", preset.row);
  setDimensionSelection("column", preset.column);
  elements.sortSelect.value = preset.sort;
  elements.limitSelect.value = preset.limit;
  clearFilters();
  runQuery();
}

function renderSummary(result) {
  const cards = [
    {
      label: result.measure.label,
      value: formatValue(result.summary.value, result.measure.format),
    },
    {
      label: "Groups returned",
      value: formatValue(result.meta.group_count, "integer"),
    },
    {
      label: "Query time",
      value: formatValue(result.meta.elapsed_ms, "integer") + " ms",
    },
  ];
  elements.summary.innerHTML = cards
    .map(
      (card) =>
        '<div class="card"><span>' +
        escapeHtml(card.label) +
        "</span><strong>" +
        escapeHtml(card.value) +
        "</strong></div>"
    )
    .join("");
}

function renderChart(result) {
  let entries = [];
  if (result.pivot) {
    if (!result.measure.additive) {
      elements.chart.innerHTML = "";
      return;
    }
    entries = result.pivot.rows.map((row) => ({ label: row.label, value: row.total }));
  } else {
    entries = result.rows.map((row) => ({ label: String(row.row), value: row.metric }));
  }
  entries = entries
    .filter((entry) => entry.value !== null && entry.value !== undefined)
    .slice(0, 15);
  if (!entries.length) {
    elements.chart.innerHTML = "";
    return;
  }
  const maxAbs = Math.max(...entries.map((entry) => Math.abs(entry.value)), 1);
  const rows = entries
    .map((entry) => {
      const width = Math.max(1, (Math.abs(entry.value) / maxAbs) * 100);
      const negative = entry.value < 0;
      return (
        '<div class="bar-row">' +
        '<span class="bar-label" title="' + escapeHtml(entry.label) + '">' + escapeHtml(entry.label) + "</span>" +
        '<div class="bar-track"><div class="bar' + (negative ? " negative" : "") + '" style="width:' + width.toFixed(2) + '%"></div></div>' +
        '<span class="bar-value">' + escapeHtml(formatValue(entry.value, result.measure.format)) + "</span>" +
        "</div>"
      );
    })
    .join("");
  elements.chart.innerHTML =
    '<div class="chart-title">Top ' + entries.length + " groups by " + escapeHtml(result.measure.label.toLowerCase()) + "</div>" + rows;
}

function renderTable(result) {
  elements.notice.innerHTML = "";
  if (result.pivot) {
    const pivot = result.pivot;
    const headerCells = pivot.columns
      .map((column) => '<th class="num">' + escapeHtml(column.label) + "</th>")
      .join("");
    const totalHeader = result.measure.additive ? '<th class="num">Total</th>' : "";
    const bodyRows = pivot.rows
      .map((row) => {
        const cells = pivot.columns
          .map(
            (column) =>
              '<td class="num">' +
              escapeHtml(formatValue(row.values[column.key], result.measure.format)) +
              "</td>"
          )
          .join("");
        const totalCell = result.measure.additive
          ? '<td class="num"><strong>' + escapeHtml(formatValue(row.total, result.measure.format)) + "</strong></td>"
          : "";
        return "<tr><td>" + escapeHtml(row.label) + "</td>" + cells + totalCell + "</tr>";
      })
      .join("");
    let totalRow = "";
    if (result.measure.additive) {
      const totalCells = pivot.columns
        .map(
          (column) =>
            '<td class="num">' +
            escapeHtml(formatValue(pivot.column_totals[column.key], result.measure.format)) +
            "</td>"
        )
        .join("");
      totalRow =
        '<tr class="total-row"><td>Grand total</td>' +
        totalCells +
        '<td class="num">' +
        escapeHtml(formatValue(pivot.grand_total, result.measure.format)) +
        "</td></tr>";
    }
    elements.tableWrap.innerHTML =
      '<table><thead><tr><th>' +
      escapeHtml(pivot.row_label) +
      "</th>" +
      headerCells +
      totalHeader +
      "</tr></thead><tbody>" +
      bodyRows +
      totalRow +
      "</tbody></table>";
    if (result.meta.truncated) {
      elements.notice.innerHTML =
        '<p class="notice">Results were capped to protect the app. Add filters or choose fewer levels.</p>';
    }
    return;
  }

  const rowLabel = result.row ? result.row.label : "Scope";
  const bodyRows = result.rows
    .map(
      (row) =>
        "<tr><td>" +
        escapeHtml(row.row) +
        '</td><td class="num">' +
        escapeHtml(formatValue(row.metric, result.measure.format)) +
        "</td></tr>"
    )
    .join("");
  elements.tableWrap.innerHTML =
    '<table><thead><tr><th>' +
    escapeHtml(rowLabel) +
    '</th><th class="num">' +
    escapeHtml(result.measure.label) +
    "</th></tr></thead><tbody>" +
    bodyRows +
    "</tbody></table>";
  if (result.meta.truncated) {
    elements.notice.innerHTML =
      '<p class="notice">Results were capped at ' +
      escapeHtml(result.meta.group_count) +
      " rows. Add filters or raise the row limit.</p>";
  }
}

function renderResult(result) {
  state.lastResult = result;
  elements.exportBtn.disabled = false;
  const rowLabel = result.row ? result.row.label : "Grand total";
  const columnLabel = result.column ? " by " + result.column.label : "";
  elements.resultTitle.textContent = result.measure.label + " by " + rowLabel + columnLabel;
  const parts = [
    result.meta.group_count + " groups",
    result.meta.elapsed_ms + " ms",
  ];
  if (result.column) parts.push(result.meta.column_count + " columns");
  elements.queryInfo.textContent = parts.join(" · ");
  renderSummary(result);
  renderChart(result);
  renderTable(result);
}

function showError(message) {
  setStatus(false, "Query error");
  elements.summary.innerHTML = "";
  elements.chart.innerHTML = "";
  elements.queryInfo.textContent = "";
  elements.resultTitle.textContent = "Results";
  elements.notice.innerHTML = "";
  elements.tableWrap.innerHTML = '<div class="error">' + escapeHtml(message) + "</div>";
  state.lastResult = null;
  elements.exportBtn.disabled = true;
}

async function runQuery() {
  if (!state.metadata) return;
  setLoading(true);
  elements.tableWrap.innerHTML = '<div class="empty">Running OLAP query...</div>';
  try {
    const response = await fetch("/api/query", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(buildPayload()),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Query failed.");
    setStatus(true, "Connected");
    renderResult(payload);
  } catch (error) {
    showError(error.message);
  } finally {
    setLoading(false);
  }
}

function csvCell(value) {
  if (value === null || value === undefined) return "";
  const text = String(value).replaceAll('"', '""');
  return '"' + text + '"';
}

function exportCsv() {
  const result = state.lastResult;
  if (!result) return;
  const lines = [];
  if (result.pivot) {
    const columns = result.pivot.columns;
    const header = ["Row", ...columns.map((column) => column.label)];
    if (result.measure.additive) header.push("Total");
    lines.push(header.map(csvCell).join(","));
    for (const row of result.pivot.rows) {
      const values = [row.label, ...columns.map((column) => row.values[column.key])];
      if (result.measure.additive) values.push(row.total);
      lines.push(values.map(csvCell).join(","));
    }
    if (result.measure.additive) {
      const totals = ["Grand total", ...columns.map((column) => result.pivot.column_totals[column.key]), result.pivot.grand_total];
      lines.push(totals.map(csvCell).join(","));
    }
  } else {
    lines.push([result.row ? result.row.label : "Scope", result.measure.label].map(csvCell).join(","));
    for (const row of result.rows) {
      lines.push([row.row, row.metric].map(csvCell).join(","));
    }
  }
  const blob = new Blob([lines.join("\\n")], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = "olap-result.csv";
  anchor.click();
  URL.revokeObjectURL(url);
}

async function loadMetadata() {
  setStatus(false, "Checking");
  try {
    const response = await fetch("/api/metadata");
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Could not load metadata.");
    state.metadata = payload;
    const dateRange = payload.date_range;
    elements.connectionText.textContent =
      payload.database +
      " · " +
      new Intl.NumberFormat("en-US").format(payload.fact_rows) +
      " fact rows · " +
      dateRange.min +
      " to " +
      dateRange.max;
    fillSelect(
      elements.measureSelect,
      payload.measures.map((measure) => ({
        key: measure.key,
        label: measure.label,
      })),
      false
    );
    fillSelect(
      elements.rowDimension,
      Object.entries(payload.dimensions).map(([key, dimension]) => ({
        key,
        label: dimension.label,
      })),
      true
    );
    fillSelect(
      elements.columnDimension,
      Object.entries(payload.dimensions).map(([key, dimension]) => ({
        key,
        label: dimension.label,
      })),
      true
    );
    renderFilters();
    setStatus(true, "Connected");
    applyPreset("yearly");
  } catch (error) {
    elements.connectionText.textContent = "Data warehouse unavailable";
    showError(error.message);
  }
}

elements.rowDimension.addEventListener("change", () => {
  updateLevels(elements.rowDimension.value, elements.rowLevel, null);
});
elements.columnDimension.addEventListener("change", () => {
  updateLevels(elements.columnDimension.value, elements.columnLevel, null);
});
elements.runBtn.addEventListener("click", runQuery);
elements.resetBtn.addEventListener("click", () => applyPreset("yearly"));
elements.exportBtn.addEventListener("click", exportCsv);
document.querySelectorAll("[data-preset]").forEach((button) => {
  button.addEventListener("click", () => applyPreset(button.dataset.preset));
});

loadMetadata();
</script>
</body>
</html>
"""


class OlapHandler(BaseHTTPRequestHandler):
    server_version = "FinancialOlap/1.0"

    def _send_bytes(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status, payload):
        body = json.dumps(payload, default=str).encode("utf-8")
        self._send_bytes(status, "application/json; charset=utf-8", body)

    def _send_error_json(self, status, message):
        self._send_json(status, {"error": message})

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self._send_bytes(
                HTTPStatus.OK,
                "text/html; charset=utf-8",
                INDEX_HTML.encode("utf-8"),
            )
        elif path == "/api/metadata":
            try:
                self._send_json(HTTPStatus.OK, get_metadata())
            except Exception as exc:
                self._send_error_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    f"Could not load metadata: {exc}",
                )
        elif path == "/api/health":
            try:
                with engine.connect() as connection:
                    connection.execute(text("SELECT 1"))
                self._send_json(HTTPStatus.OK, {"status": "ok", "database": DB_NAME})
            except Exception as exc:
                self._send_error_json(HTTPStatus.SERVICE_UNAVAILABLE, str(exc))
        elif path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
        else:
            self._send_error_json(HTTPStatus.NOT_FOUND, "Not found.")

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/api/query":
            self._send_error_json(HTTPStatus.NOT_FOUND, "Not found.")
            return
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length else b"{}"
            payload = json.loads(body.decode("utf-8"))
            self._send_json(HTTPStatus.OK, run_query(payload))
        except ValueError as exc:
            self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        except json.JSONDecodeError:
            self._send_error_json(HTTPStatus.BAD_REQUEST, "Invalid JSON payload.")
        except Exception as exc:
            self._send_error_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                f"Query failed: {exc}",
            )

    def log_message(self, format_string, *args):
        print(
            f"[{self.log_date_time_string()}] {self.address_string()} "
            + format_string % args
        )


def check_connection():
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Run the Financial OLAP Explorer web app."
    )
    parser.add_argument(
        "--host",
        default=os.getenv("OLAP_HOST", "127.0.0.1"),
        help="Interface to bind to (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("OLAP_PORT", "8000")),
        help="Port to listen on (default: 8000).",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="Open the app in the default browser.",
    )
    args = parser.parse_args()

    try:
        check_connection()
        print(f"Connected to MySQL database '{DB_NAME}' at {DB_HOST}:{DB_PORT}.")
    except Exception as exc:
        print(f"Warning: could not connect to MySQL: {exc}")

    server = ThreadingHTTPServer((args.host, args.port), OlapHandler)
    url = f"http://{args.host}:{args.port}"
    print(f"Financial OLAP Explorer running at {url}")
    print("Press Ctrl+C to stop.")

    if args.open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server.")
    finally:
        server.server_close()
        engine.dispose()


if __name__ == "__main__":
    main()
