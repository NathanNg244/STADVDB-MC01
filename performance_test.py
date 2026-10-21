"""Measure the OLAP queries in olap_queries.sql and optionally collect EXPLAIN plans.

Compatible with the App.py command line:
    python performance_test.py --label before --runs 5 --warmups 1 --output results --explain

This script writes one label's measurements and execution plans at a time.
It does not compare reports or calculate optimization improvement.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import platform
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

import sqlalchemy
from sqlalchemy import create_engine, text

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # Python 3.10, if tomli is installed
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        tomllib = None


PROJECT_DIR = Path(__file__).resolve().parent
SQL_FILE = PROJECT_DIR / "olap_queries.sql"
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "results"


# ----------------------------------------------------------
# CONNECTION SETTINGS
# ----------------------------------------------------------

def load_mysql_settings():
    """Read .streamlit/secrets.toml; fall back to Streamlit secrets."""
    secrets_file = PROJECT_DIR / ".streamlit" / "secrets.toml"

    if secrets_file.is_file() and tomllib is not None:
        with secrets_file.open("rb") as file:
            settings = tomllib.load(file)
        if "mysql" not in settings:
            raise KeyError(
                f"The file {secrets_file} must contain a [mysql] section."
            )
        return settings["mysql"]

    # This fallback also supports environments where secrets are configured
    # by Streamlit rather than stored in the project's local TOML file.
    try:
        import streamlit as st

        return dict(st.secrets["mysql"])
    except Exception as exc:
        if tomllib is None:
            detail = (
                "Python tomllib is unavailable and tomli is not installed. "
                "Use Python 3.11+ or install tomli."
            )
        else:
            detail = (
                "Could not read .streamlit/secrets.toml or Streamlit secrets."
            )
        raise RuntimeError(f"{detail} Original error: {exc}") from exc


def create_db_engine():
    db = load_mysql_settings()
    required = ("user", "password", "host", "port", "database")
    missing = [key for key in required if key not in db]
    if missing:
        raise KeyError(
            "Missing database settings: " + ", ".join(missing)
        )

    connection_url = (
        f"mysql+pymysql://{quote_plus(str(db['user']))}:"
        f"{quote_plus(str(db['password']))}@{db['host']}:"
        f"{db['port']}/{db['database']}"
    )

    return create_engine(
        connection_url,
        pool_pre_ping=True,
        connect_args={"ssl": {}},
    )


# ----------------------------------------------------------
# SQL LOADING AND WORKLOAD VARIANTS
# ----------------------------------------------------------

def load_olap_queries(sql_file: Path = SQL_FILE) -> list[str]:
    """Load the four SELECT statements, ignoring comments and USE database."""
    if not sql_file.is_file():
        raise FileNotFoundError(f"SQL file not found: {sql_file}")

    source = sql_file.read_text(encoding="utf-8-sig")
    lines = [
        line for line in source.splitlines()
        if not line.strip().startswith("--")
    ]
    source = "\n".join(lines)

    statements = []
    for part in source.split(";"):
        statement = part.strip()
        if not statement:
            continue
        if statement.upper().startswith("USE "):
            continue
        # The benchmark file is expected to contain SELECT queries only.
        if not statement.upper().startswith("SELECT"):
            raise ValueError(
                "Unexpected non-SELECT statement in the OLAP SQL file: "
                + statement.splitlines()[0][:100]
            )
        statements.append(statement)

    if len(statements) != 4:
        raise ValueError(
            f"Expected 4 OLAP SELECT queries in {sql_file.name}; "
            f"found {len(statements)}. Check comments and semicolons."
        )

    return statements


def inject_where_before_group_by(sql: str, predicate: str) -> str:
    """Insert a WHERE condition before GROUP BY in an unfiltered query."""
    import re

    match = re.search(r"\bGROUP\s+BY\b", sql, flags=re.IGNORECASE)
    if not match:
        raise ValueError("Could not find GROUP BY in query variant.")
    return sql[:match.start()] + f"WHERE {predicate}\n" + sql[match.start():]


def add_date_filter_to_district_rollup(sql: str, years: list[int]) -> str:
    """Add a date dimension join and a year filter to a District Roll Up variant."""
    import re

    sql, count = re.subn(
        r"(\bFROM\s+FactTransaction\s+f\b)",
        r"\1\nJOIN DimDate d ON d.date_key = f.date_key\n",
        sql,
        count=1,
        flags=re.IGNORECASE,
    )
    if count != 1:
        raise ValueError("Could not add DimDate join to District Roll Up.")

    values = ", ".join(str(int(year)) for year in years)
    return inject_where_before_group_by(
        sql, f"d.year_number IN ({values})\n"
    )


def replace_year_equality(sql: str, year: int) -> str:
    import re

    result, count = re.subn(
        r"(\bd\.year_number\s*=\s*)\d+",
        lambda match: match.group(1) + str(int(year)),
        sql,
        count=1,
        flags=re.IGNORECASE,
    )
    if count != 1:
        raise ValueError("Could not replace the Slice year filter.")
    return result


def replace_year_list(sql: str, years: list[int]) -> str:
    import re

    values = ", ".join(str(int(year)) for year in years)
    result, count = re.subn(
        r"(\bd\.year_number\s+IN\s*\()[^)]*(\))",
        lambda match: match.group(1) + values + match.group(2),
        sql,
        count=1,
        flags=re.IGNORECASE,
    )
    if count != 1:
        raise ValueError("Could not replace the Dice year filter.")
    return result


def replace_quarter_list(sql: str, quarters: list[int]) -> str:
    import re

    values = ", ".join(str(int(quarter)) for quarter in quarters)
    result, count = re.subn(
        r"(\bd\.quarter_number\s+IN\s*\()[^)]*(\))",
        lambda match: match.group(1) + values + match.group(2),
        sql,
        count=1,
        flags=re.IGNORECASE,
    )
    if count != 1:
        raise ValueError("Could not replace the Dice quarter filter.")
    return result


def sql_string_literal(value: str) -> str:
    """Quote a database-derived string for a literal in a diagnostic workload."""
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def replace_region_list(sql: str, regions: list[str]) -> str:
    import re

    if not regions:
        return sql
    values = ", ".join(sql_string_literal(region) for region in regions)
    result, count = re.subn(
        r"(\br\.region\s+IN\s*\()[^)]*(\))",
        lambda match: match.group(1) + values + match.group(2),
        sql,
        count=1,
        flags=re.IGNORECASE,
    )
    if count != 1:
        raise ValueError("Could not replace the Dice region filter.")
    return result


def replace_frequency_list(sql: str, frequencies: list[str]) -> str:
    import re

    if not frequencies:
        return sql
    values = ", ".join(sql_string_literal(item) for item in frequencies)
    result, count = re.subn(
        r"(\ba\.frequency\s+IN\s*\()[^)]*(\))",
        lambda match: match.group(1) + values + match.group(2),
        sql,
        count=1,
        flags=re.IGNORECASE,
    )
    if count != 1:
        raise ValueError("Could not replace the Dice frequency filter.")
    return result


def discover_values(engine, sql: str, column: str) -> list[str]:
    """Return a short list of distinct dimension values for parameter cases."""
    # sql/column are internal fixed strings, not user-provided SQL.
    with engine.connect() as connection:
        rows = connection.execute(text(sql)).fetchall()
    return [str(row[0]) for row in rows if row[0] is not None]


def build_workloads(engine, queries: list[str]) -> list[dict]:
    """Build repeatable workloads from the four queries and parameter variants."""
    with engine.connect() as connection:
        years_rows = connection.execute(text("""
            SELECT DISTINCT year_number
            FROM DimDate
            WHERE year_number IS NOT NULL
            ORDER BY year_number
        """)).fetchall()
    years = [int(row[0]) for row in years_rows]
    if not years:
        raise RuntimeError("DimDate has no non-NULL year_number values.")

    early_year = years[0]
    late_year = years[-1]
    two_years = years[-2:] if len(years) >= 2 else years

    regions = discover_values(
        engine,
        "SELECT DISTINCT region FROM DimDistrict "
        "WHERE region IS NOT NULL ORDER BY region LIMIT 10",
        "region",
    )
    frequencies = discover_values(
        engine,
        "SELECT DISTINCT frequency FROM DimAccount "
        "WHERE frequency IS NOT NULL ORDER BY frequency LIMIT 10",
        "frequency",
    )

    workloads = []

    def add(name: str, scenario: str, sql: str, parameters: str):
        workloads.append({
            "query_name": name,
            "scenario": scenario,
            "parameters": parameters,
            "sql": sql.strip().rstrip(";"),
        })

    # Query 1: Time Roll Up; extra variants filter the same date hierarchy.
    add("Time Roll Up", "all_years", queries[0], "all available years")
    add(
        "Time Roll Up", "single_year",
        inject_where_before_group_by(
            queries[0], f"d.year_number = {early_year}\n"
        ),
        f"year={early_year}",
    )
    add(
        "Time Roll Up", "two_recent_years",
        inject_where_before_group_by(
            queries[0],
            "d.year_number IN (" + ", ".join(map(str, two_years)) + ")\n",
        ),
        f"years={two_years}",
    )

    # Query 2: District Roll Up; date filters are additional parameter cases.
    add(
        "District Roll Up", "all_years", queries[1],
        "all available years",
    )
    add(
        "District Roll Up", "single_year",
        add_date_filter_to_district_rollup(queries[1], [early_year]),
        f"year={early_year}",
    )
    add(
        "District Roll Up", "two_recent_years",
        add_date_filter_to_district_rollup(queries[1], two_years),
        f"years={two_years}",
    )

    # Query 3: Slice; retain the HAVING threshold and change its year value.
    add("Slice", "year_1997_original", queries[2], "year=1997; count>=100")
    if early_year != 1997:
        add(
            "Slice", "earliest_year",
            replace_year_equality(queries[2], early_year),
            f"year={early_year}; count>=100",
        )
    if late_year != 1997 and late_year != early_year:
        add(
            "Slice", "latest_year",
            replace_year_equality(queries[2], late_year),
            f"year={late_year}; count>=100",
        )

    # Query 4: Dice; change years, quarters, and where possible dimension filters.
    add("Dice", "original_filters", queries[3], "original SQL filters")
    dice_years = two_years
    add(
        "Dice", "recent_years",
        replace_year_list(queries[3], dice_years),
        f"years={dice_years}; original quarter/region/type/frequency filters",
    )
    add(
        "Dice", "quarters_3_4",
        replace_quarter_list(queries[3], [3, 4]),
        "original years; quarters=[3, 4]",
    )
    if regions:
        add(
            "Dice", "single_region",
            replace_region_list(queries[3], [regions[0]]),
            f"region={regions[0]!r}; other original filters",
        )
    if frequencies:
        # Only create this variant if a real value differs from Weekly.
        alternative_frequency = next(
            (value for value in frequencies if value.lower() != "weekly"),
            None,
        )
        if alternative_frequency:
            add(
                "Dice", "alternate_frequency",
                replace_frequency_list(queries[3], [alternative_frequency]),
                f"frequency={alternative_frequency!r}; other original filters",
            )

    return workloads


# ----------------------------------------------------------
# BENCHMARKING AND EXPLAIN
# ----------------------------------------------------------

def benchmark_one(engine, sql: str) -> tuple[float, int]:
    """Measure query execution plus complete result fetching, excluding connect time."""
    with engine.connect() as connection:
        started = time.perf_counter()
        result = connection.execute(text(sql))
        rows = result.fetchall()
        elapsed_ms = (time.perf_counter() - started) * 1000.0
    return elapsed_ms, len(rows)


def _walk_dicts(value):
    """Yield every dictionary nested in a JSON object/list."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def format_explain_json_plan(raw_plan) -> str:
    """Render a concise human-readable summary of a MySQL JSON plan."""
    if isinstance(raw_plan, str):
        try:
            plan = json.loads(raw_plan)
        except json.JSONDecodeError:
            return "EXPLAIN returned text that was not valid JSON:\n\n" + raw_plan
    else:
        plan = raw_plan

    dictionaries = list(_walk_dicts(plan))
    query_cost = next(
        (
            item.get("cost_info", {}).get("query_cost")
            for item in dictionaries
            if isinstance(item.get("cost_info"), dict)
            and item.get("cost_info", {}).get("query_cost") is not None
        ),
        None,
    )
    uses_temp_table = any(
        item.get("using_temporary_table") is True for item in dictionaries
    )
    uses_filesort = any(
        item.get("using_filesort") is True for item in dictionaries
    )

    tables = []
    seen_tables = set()
    for item in dictionaries:
        table = item.get("table")
        if not isinstance(table, dict) or "table_name" not in table:
            continue
        identity = (
            table.get("table_name"),
            table.get("access_type"),
            table.get("key"),
        )
        if identity in seen_tables:
            continue
        seen_tables.add(identity)
        tables.append(table)

    lines = [
        "PLAN SUMMARY",
        "-" * 78,
        f"Estimated query cost : {query_cost if query_cost is not None else 'Not provided'}",
        f"Temporary table      : {'YES' if uses_temp_table else 'No flag reported'}",
        f"Filesort             : {'YES' if uses_filesort else 'No flag reported'}",
        "",
        "TABLE ACCESS",
        "-" * 78,
    ]

    if tables:
        headings = ["Table", "Access", "Index used", "Rows estimate", "Filtered", "Prefix cost"]
        table_rows = []
        for table in tables:
            cost_info = table.get("cost_info") or {}
            table_rows.append([
                str(table.get("table_name", "-")),
                str(table.get("access_type", "-")),
                str(table.get("key", "(none)")),
                str(table.get("rows_examined_per_scan", "-")),
                str(table.get("filtered", "-")),
                str(cost_info.get("prefix_cost", "-")),
            ])

        widths = [
            max(len(headings[i]), *(len(row[i]) for row in table_rows))
            for i in range(len(headings))
        ]
        lines.append(" | ".join(headings[i].ljust(widths[i]) for i in range(len(headings))))
        lines.append("-+-".join("-" * width for width in widths))
        for row in table_rows:
            lines.append(" | ".join(row[i].ljust(widths[i]) for i in range(len(row))))
    else:
        lines.append("No table-access nodes were found in the returned plan.")

    lines.extend([
        "",
        "INTERPRETATION NOTES",
        "-" * 78,
        "- Estimated query cost is an optimizer estimate, not milliseconds.",
        "- ALL usually indicates a full table scan; ref/eq_ref commonly indicate indexed lookup.",
        "- Temporary-table/filesort flags are investigation leads, not proof of a bottleneck by themselves.",
        "- Row counts are estimates. Use the benchmark CSV for measured execution time.",
    ])
    return "\n".join(lines)


def collect_plan(engine, sql: str, analyze: bool = False) -> tuple[str, object | None]:
    """Return a readable text summary and, for JSON EXPLAIN, the full parsed plan."""
    command = "EXPLAIN ANALYZE" if analyze else "EXPLAIN FORMAT=JSON"
    with engine.connect() as connection:
        result = connection.execute(text(f"{command} {sql}"))
        rows = result.fetchall()

    if analyze:
        # EXPLAIN ANALYZE is returned as a text execution tree on supported MySQL versions.
        return "\n".join(str(row[0]) for row in rows), None

    if not rows:
        return "EXPLAIN returned no rows.", None

    # MySQL commonly returns the JSON plan in a single column named EXPLAIN.
    first_row = rows[0]._mapping
    raw_plan = first_row.get("EXPLAIN")
    if raw_plan is None and first_row:
        raw_plan = next(iter(first_row.values()))

    try:
        parsed_plan = json.loads(raw_plan) if isinstance(raw_plan, str) else raw_plan
    except json.JSONDecodeError:
        return "EXPLAIN returned non-JSON output:\n\n" + str(raw_plan), None

    return format_explain_json_plan(parsed_plan), parsed_plan


def summarize_measurements(records: list[dict]) -> list[dict]:
    grouped: dict[tuple[str, str], list[dict]] = {}
    for record in records:
        grouped.setdefault(
            (record["query_name"], record["scenario"]), []
        ).append(record)

    summary = []
    for (query_name, scenario), items in grouped.items():
        times = [float(item["elapsed_ms"]) for item in items]
        summary.append({
            "query_name": query_name,
            "scenario": scenario,
            "runs": len(times),
            "min_ms": min(times),
            "median_ms": statistics.median(times),
            "mean_ms": statistics.mean(times),
            "max_ms": max(times),
            "stdev_ms": statistics.pstdev(times) if len(times) > 1 else 0.0,
            "mean_rows_returned": statistics.mean(
                int(item["rows_returned"]) for item in items
            ),
            "sql_sha256": items[0]["sql_sha256"],
        })
    return summary


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys = list(dict.fromkeys(key for row in rows for key in row.keys()))
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, content) -> None:
    path.write_text(
        json.dumps(content, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )




def parse_args():
    parser = argparse.ArgumentParser(
        description="Benchmark all OLAP SELECT queries from olap_queries.sql."
    )
    parser.add_argument(
        "--label", choices=("before", "after"), default="before",
        help="Measurement phase used in output filenames.",
    )
    parser.add_argument(
        "--runs", type=int, default=5,
        help="Number of measured executions per workload case (default: 5).",
    )
    parser.add_argument(
        "--warmups", type=int, default=1,
        help="Unrecorded warm-up executions per workload case (default: 1).",
    )
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT_DIR,
        help="Directory for JSON, CSV, and EXPLAIN reports.",
    )
    parser.add_argument(
        "--explain", action="store_true",
        help="Save MySQL EXPLAIN FORMAT=JSON plans for every workload case.",
    )
    parser.add_argument(
        "--analyze", action="store_true",
        help="Also run EXPLAIN ANALYZE where supported (executes each query).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.runs < 1:
        print("ERROR: --runs must be at least 1.", file=sys.stderr)
        return 2
    if args.warmups < 0:
        print("ERROR: --warmups cannot be negative.", file=sys.stderr)
        return 2

    if args.analyze:
        args.explain = True

    args.output.mkdir(parents=True, exist_ok=True)
    engine = None

    try:
        queries = load_olap_queries()
        engine = create_db_engine()

        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            server_info = connection.execute(text(
                "SELECT VERSION() AS mysql_version, DATABASE() AS database_name"
            )).mappings().one()

        workloads = build_workloads(engine, queries)
        now = datetime.now(timezone.utc).isoformat()
        measurements = []
        plan_sections = []
        plan_json_sections = []
        total_cases = len(workloads)

        print(f"Benchmark label: {args.label}")
        print(f"Database: {server_info['database_name']} ({server_info['mysql_version']})")
        print(f"Workload cases: {total_cases}; warm-ups/case: {args.warmups}; runs/case: {args.runs}")
        print(f"Output directory: {args.output.resolve()}")

        for case_number, workload in enumerate(workloads, start=1):
            query_name = workload["query_name"]
            scenario = workload["scenario"]
            sql = workload["sql"]
            sql_hash = hashlib.sha256(sql.encode("utf-8")).hexdigest()

            print(
                f"[{case_number}/{total_cases}] {query_name} / {scenario} "
                f"({workload['parameters']})"
            )

            # Warm-up runs are intentionally excluded from recorded timings.
            for warmup_number in range(args.warmups):
                benchmark_one(engine, sql)

            case_records = []
            for run_number in range(1, args.runs + 1):
                elapsed_ms, rows_returned = benchmark_one(engine, sql)
                record = {
                    "label": args.label,
                    "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                    "query_name": query_name,
                    "scenario": scenario,
                    "parameters": workload["parameters"],
                    "run": run_number,
                    "elapsed_ms": round(elapsed_ms, 6),
                    "rows_returned": rows_returned,
                    "sql_sha256": sql_hash,
                }
                measurements.append(record)
                case_records.append(record)
                print(f"    run {run_number}: {elapsed_ms:.3f} ms; rows={rows_returned}")

            times = [float(record["elapsed_ms"]) for record in case_records]
            print(
                f"    mean={statistics.mean(times):.3f} ms; "
                f"median={statistics.median(times):.3f} ms"
            )

            if args.explain:
                for analyze in ([False, True] if args.analyze else [False]):
                    label = "EXPLAIN ANALYZE" if analyze else "EXPLAIN FORMAT=JSON"
                    try:
                        plan_text, parsed_plan = collect_plan(
                            engine, sql, analyze=analyze
                        )
                        plan_sections.append(
                            f"\n{'=' * 90}\n{query_name} / {scenario}\n"
                            f"Parameters: {workload['parameters']}\n"
                            f"{label}\n{'-' * 90}\n{plan_text}\n"
                        )
                        if parsed_plan is not None:
                            plan_json_sections.append({
                                "query_name": query_name,
                                "scenario": scenario,
                                "parameters": workload["parameters"],
                                "explain": parsed_plan,
                            })
                    except Exception as exc:
                        message = (
                            f"{label} failed for {query_name} / {scenario}: "
                            f"{type(exc).__name__}: {exc}"
                        )
                        plan_sections.append(
                            f"\n{'=' * 90}\n{message}\n"
                        )
                        print(f"    WARNING: {message}")

        summary = summarize_measurements(measurements)
        report = {
            "label": args.label,
            "created_at_utc": now,
            "sql_file": SQL_FILE.name,
            "database": dict(server_info),
            "environment": {
                "platform": platform.platform(),
                "python_version": sys.version,
                "sqlalchemy_version": sqlalchemy.__version__,
                "runs_per_case": args.runs,
                "warmups_per_case": args.warmups,
                "explain_enabled": args.explain,
                "explain_analyze_enabled": args.analyze,
            },
            "notes": [
                "Elapsed time includes SQL execution and fetching all result rows, but excludes acquiring a pooled connection.",
                "Warm-up executions are excluded from recorded measurements.",
                "This script records measurements for the selected label only; it does not compare labels or calculate improvement.",
                "Filtered workload cases are additional parameter variants; the original SQL case is retained for each OLAP query.",
            ],
            "summary": summary,
            "measurements": measurements,
        }

        json_path = args.output / f"{args.label}.json"
        csv_path = args.output / f"{args.label}.csv"
        write_json(json_path, report)
        write_csv(csv_path, measurements)

        if args.explain:
            explain_path = args.output / f"{args.label}_explain.txt"
            explain_path.write_text(
                f"EXPLAIN SUMMARY REPORT — {args.label.upper()}\n"
                f"Database: {server_info['database_name']} ({server_info['mysql_version']})\n"
                "The text file contains concise summaries. Full JSON plans are in the companion JSON file.\n"
                + "".join(plan_sections),
                encoding="utf-8",
            )
            print(f"Readable EXPLAIN summary: {explain_path.resolve()}")

            if plan_json_sections:
                explain_json_path = args.output / f"{args.label}_explain.json"
                write_json(explain_json_path, {
                    "label": args.label,
                    "database": server_info["database_name"],
                    "mysql_version": server_info["mysql_version"],
                    "note": "Full MySQL EXPLAIN FORMAT=JSON plans for detailed inspection.",
                    "plans": plan_json_sections,
                })
                print(f"Full EXPLAIN JSON plans: {explain_json_path.resolve()}")

        print(f"JSON report: {json_path.resolve()}")
        print(f"CSV measurements: {csv_path.resolve()}")

        print("Performance benchmark completed successfully.")
        return 0

    except Exception as exc:
        print(
            f"ERROR: performance benchmark failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
