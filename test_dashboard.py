import re
import math
import unittest
from pathlib import Path
from urllib.parse import quote_plus

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text


# ==========================================================
# CONFIGURATION
# ==========================================================

PROJECT_DIR = Path(__file__).resolve().parent
OLAP_SQL_FILE = PROJECT_DIR / "olap_queries.sql"


def get_test_engine():
    """Create a MySQL connection pool using App.py credentials."""
    db = st.secrets["mysql"]

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


# ==========================================================
# DASHBOARD QUERY FUNCTIONS
# Merged from dashboard_queries.py
# ==========================================================


def get_transaction_trends(engine, selected_years, granularity):
    columns = [
        "period",
        "transaction_count",
        "amount_count",
        "total_amount",
        "average_amount",
    ]

    if not selected_years:
        return pd.DataFrame(columns=columns)

    if granularity == "Year":
        period_expression = "CAST(d.year_number AS CHAR)"
        group_columns = "d.year_number"
        order_columns = "d.year_number"

    elif granularity == "Quarter":
        period_expression = (
            "CONCAT(d.year_number, ' Q', d.quarter_number)"
        )
        group_columns = "d.year_number, d.quarter_number"
        order_columns = "d.year_number, d.quarter_number"

    elif granularity == "Month":
        period_expression = """
            CONCAT(
                d.year_number, '-',
                LPAD(d.month_number, 2, '0'),
                ' ', d.month_name
            )
        """
        group_columns = (
            "d.year_number, d.month_number, d.month_name"
        )
        order_columns = "d.year_number, d.month_number"

    else:
        raise ValueError(
            f"Unsupported granularity: {granularity}"
        )

    placeholders = ", ".join(
        f":year_{i}" for i in range(len(selected_years))
    )

    query = f"""
        SELECT
            {period_expression} AS period,
            COUNT(*) AS transaction_count,
            COUNT(f.amount) AS amount_count,
            COALESCE(SUM(f.amount), 0) AS total_amount,
            AVG(f.amount) AS average_amount
        FROM facttransaction f
        JOIN dimdate d
            ON f.date_key = d.date_key
        WHERE d.year_number IN ({placeholders})
        GROUP BY {group_columns}
        ORDER BY {order_columns}
    """

    parameters = {
        f"year_{i}": year
        for i, year in enumerate(selected_years)
    }

    return pd.read_sql(
        text(query), engine, params=parameters
    )


def summarize_transaction_trends(trend_df):
    if trend_df.empty:
        return {
            "total_count": 0,
            "total_amount": 0.0,
            "average_amount": 0.0,
        }

    total_count = int(
        trend_df["transaction_count"].sum()
    )
    total_amount = float(
        trend_df["total_amount"].sum()
    )
    amount_count = int(
        trend_df["amount_count"].sum()
    )

    return {
        "total_count": total_count,
        "total_amount": total_amount,
        "average_amount": (
            total_amount / amount_count
            if amount_count
            else 0.0
        ),
    }


# ==========================================================
# SQL FILE LOADER
# ==========================================================


def load_olap_queries():
    """Load the four SELECT/CTE queries from olap_queries.sql."""
    if not OLAP_SQL_FILE.is_file():
        raise FileNotFoundError(
            f"SQL file not found: {OLAP_SQL_FILE}"
        )

    source = OLAP_SQL_FILE.read_text(encoding="utf-8-sig")

    # Remove full-line SQL comments.
    cleaned_lines = [
        line
        for line in source.splitlines()
        if not line.strip().startswith("--")
    ]
    source = "\n".join(cleaned_lines)

    statements = []

    for statement in source.split(";"):
        statement = statement.strip()
        if not statement:
            continue

        # Ignore database-selection statements such as USE financial_dw;
        if statement.upper().startswith("USE "):
            continue

        statements.append(statement)

    if len(statements) != 4:
        raise RuntimeError(
            "Expected exactly four OLAP SELECT queries, "
            f"but found {len(statements)} in {OLAP_SQL_FILE.name}."
        )

    invalid_statements = [
        statement
        for statement in statements
        if not statement.lstrip().upper().startswith(("SELECT", "WITH"))
    ]

    if invalid_statements:
        first_line = invalid_statements[0].splitlines()[0][:100]
        raise RuntimeError(
            "The OLAP SQL file contains an unexpected "
            "non-SELECT statement: " + first_line
        )

    return statements


# ==========================================================
# TEST HELPERS
# ==========================================================


class TestDashboard(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.engine = get_test_engine()

        try:
            with cls.engine.connect() as connection:
                connection.execute(text("SELECT 1"))

            cls.olap_queries = load_olap_queries()
        except Exception:
            cls.engine.dispose()
            raise

    @classmethod
    def tearDownClass(cls):
        # get_test_engine() creates a dedicated engine for this test process.
        cls.engine.dispose()

    def independent_rows(self, sql, params=None):
        """Execute independent reference SQL."""
        with self.engine.connect() as connection:
            result = connection.execute(text(sql), params or {})
            return [dict(row) for row in result.mappings().all()]

    def execute_olap(self, index, sql_override=None):
        """Execute an OLAP query and return its rows."""
        sql = (
            sql_override
            if sql_override is not None
            else self.olap_queries[index]
        )

        with self.engine.connect() as connection:
            result = connection.execute(text(sql))
            return [dict(row) for row in result.mappings().all()]

    def assert_number_equal(self, actual, expected, tolerance=1e-5):
        self.assertIsNotNone(actual)
        self.assertIsNotNone(expected)

        self.assertTrue(
            math.isclose(
                float(actual),
                float(expected),
                rel_tol=1e-7,
                abs_tol=tolerance,
            ),
            f"Expected {expected}, got {actual}",
        )

    def assert_slice_rows_match(self, actual, expected):
        """Compare Slice aggregates by region/type, independent of row order."""
        key_fields = ("region", "type")
        numeric_fields = (
            "total_volume",
            "avg_amount",
            "largest_trans",
        )

        def index_rows(rows):
            indexed = {}
            for row in rows:
                key = tuple(str(row[field]) for field in key_fields)
                self.assertNotIn(
                    key, indexed,
                    f"Duplicate Slice group: {key}",
                )
                indexed[key] = row
            return indexed

        actual_by_key = index_rows(actual)
        expected_by_key = index_rows(expected)
        actual_keys = set(actual_by_key)
        expected_keys = set(expected_by_key)

        missing = sorted(expected_keys - actual_keys)
        unexpected = sorted(actual_keys - expected_keys)

        self.assertEqual(
            actual_keys,
            expected_keys,
            "Slice groups differ.\n"
            f"Missing groups: {missing[:20]}\n"
            f"Unexpected groups: {unexpected[:20]}",
        )

        for key in sorted(expected_keys):
            actual_row = actual_by_key[key]
            expected_row = expected_by_key[key]

            with self.subTest(region=key[0], type=key[1]):
                self.assertEqual(
                    int(actual_row["trans_count"]),
                    int(expected_row["trans_count"]),
                )
                for field in numeric_fields:
                    self.assert_number_equal(
                        actual_row[field], expected_row[field]
                    )

    def replace_slice_year_filter(self, sql, year):
        """Replace the year condition in the Slice WHERE clause."""
        where_match = re.search(
            r"\bWHERE\b(.*?)(?=\bGROUP\s+BY\b)",
            sql,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not where_match:
            self.fail("Could not find WHERE clause in Slice SQL.")

        where_clause = where_match.group(1)
        updated_where, replacements = re.subn(
            r"(\bd\.year_number\s*=\s*)\d+\b",
            lambda match: match.group(1) + str(int(year)),
            where_clause,
            count=1,
            flags=re.IGNORECASE,
        )

        if replacements != 1:
            self.fail(
                "Could not replace the year condition inside the Slice WHERE clause."
            )

        start, end = where_match.span(1)
        return sql[:start] + updated_where + sql[end:]

    def independent_slice_rows(self, source_sql, year):
        """Recompute Slice results independently, preserving its SQL semantics.

        The source Slice query aggregates facts by district and transaction type
        before rolling them up to region/type. Its HAVING COUNT(a.trans_count)
        therefore counts rows from that intermediate aggregate, not raw fact
        transactions. Recreate that intermediate grain in the reference query
        so the HAVING condition is valid and semantically equivalent.
        """
        where_match = re.search(
            r"\bWHERE\b(.*?)(?=\bGROUP\s+BY\b)",
            source_sql,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not where_match:
            self.fail("Could not extract Slice WHERE conditions.")

        where_clause = where_match.group(1).strip()
        where_clause, replacements = re.subn(
            r"(\bd\.year_number\s*=\s*)\d+\b",
            r"\1:year",
            where_clause,
            count=1,
            flags=re.IGNORECASE,
        )
        if replacements != 1:
            self.fail(
                "Could not parameterize the Slice year condition."
            )

        # Keep the source query's HAVING condition. Alias `a` is defined in
        # the outer query below as the independent intermediate aggregation.
        having_match = re.search(
            r"\bHAVING\b(.*?)(?=\bORDER\s+BY\b|;\s*$|$)",
            source_sql,
            flags=re.IGNORECASE | re.DOTALL,
        )
        having_clause = ""
        if having_match:
            condition = having_match.group(1).strip()
            if condition:
                having_clause = f"\nHAVING {condition}"

        reference_sql = f"""
            WITH ReferenceFactAgg AS (
                SELECT
                    f.district_key,
                    f.transaction_type_key,
                    COUNT(*) AS trans_count,
                    COUNT(f.amount) AS amount_count,
                    SUM(f.amount) AS amount_sum,
                    MAX(f.amount) AS largest_trans
                FROM FactTransaction f
                JOIN DimDate d
                    ON d.date_key = f.date_key
                WHERE {where_clause}
                GROUP BY f.district_key, f.transaction_type_key
            )
            SELECT
                r.region AS region,
                t.type AS type,
                SUM(a.trans_count) AS trans_count,
                ROUND(SUM(a.amount_sum), 2) AS total_volume,
                ROUND(
                    SUM(a.amount_sum) / NULLIF(SUM(a.amount_count), 0),
                    2
                ) AS avg_amount,
                ROUND(MAX(a.largest_trans), 2) AS largest_trans
            FROM ReferenceFactAgg a
            JOIN DimDistrict r
                ON r.district_key = a.district_key
            JOIN DimTransactionType t
                ON t.transaction_type_key = a.transaction_type_key
            GROUP BY r.region, t.type
            {having_clause}
            ORDER BY r.region, total_volume DESC
        """

        return self.independent_rows(
            reference_sql,
            {"year": int(year)},
        )

    def available_years(self):
        rows = self.independent_rows("""
            SELECT DISTINCT year_number
            FROM dimdate
            WHERE year_number IS NOT NULL
            ORDER BY year_number
        """)
        return [int(row["year_number"]) for row in rows]


# ==========================================================
# TD-01: TRANSACTION TRENDS
# Multiple time granularities and parameter values
# ==========================================================

    def test_dashboard_rows_match_sql(self):
        years = self.available_years()
        if not years:
            self.skipTest("No years available.")

        year_cases = [years]
        year_cases.extend([[year] for year in years])
        if len(years) >= 2:
            year_cases.append(years[:2])

        unique_cases = []
        for case in year_cases:
            if case not in unique_cases:
                unique_cases.append(case)

        for selected_years in unique_cases:
            placeholders = ", ".join(
                f":year_{i}" for i in range(len(selected_years))
            )
            params = {
                f"year_{i}": year
                for i, year in enumerate(selected_years)
            }

            reference_queries = {
                "Year": f"""
                    SELECT
                        CAST(d.year_number AS CHAR) AS period,
                        COUNT(*) AS transaction_count,
                        COUNT(f.amount) AS amount_count,
                        COALESCE(SUM(f.amount), 0) AS total_amount,
                        AVG(f.amount) AS average_amount
                    FROM facttransaction f
                    JOIN dimdate d ON f.date_key = d.date_key
                    WHERE d.year_number IN ({placeholders})
                    GROUP BY d.year_number
                    ORDER BY d.year_number
                """,
                "Quarter": f"""
                    SELECT
                        CONCAT(d.year_number, ' Q', d.quarter_number) AS period,
                        COUNT(*) AS transaction_count,
                        COUNT(f.amount) AS amount_count,
                        COALESCE(SUM(f.amount), 0) AS total_amount,
                        AVG(f.amount) AS average_amount
                    FROM facttransaction f
                    JOIN dimdate d ON f.date_key = d.date_key
                    WHERE d.year_number IN ({placeholders})
                    GROUP BY d.year_number, d.quarter_number
                    ORDER BY d.year_number, d.quarter_number
                """,
                "Month": f"""
                    SELECT
                        CONCAT(
                            d.year_number, '-',
                            LPAD(d.month_number, 2, '0'),
                            ' ', d.month_name
                        ) AS period,
                        COUNT(*) AS transaction_count,
                        COUNT(f.amount) AS amount_count,
                        COALESCE(SUM(f.amount), 0) AS total_amount,
                        AVG(f.amount) AS average_amount
                    FROM facttransaction f
                    JOIN dimdate d ON f.date_key = d.date_key
                    WHERE d.year_number IN ({placeholders})
                    GROUP BY d.year_number, d.month_number, d.month_name
                    ORDER BY d.year_number, d.month_number
                """,
            }

            for granularity, sql in reference_queries.items():
                with self.subTest(
                    years=selected_years,
                    granularity=granularity,
                ):
                    actual = get_transaction_trends(
                        self.engine, selected_years, granularity
                    ).to_dict(orient="records")
                    expected = self.independent_rows(sql, params)

                    self.assertEqual(
                        len(actual), len(expected),
                        f"Row count differs for {granularity}, years={selected_years}",
                    )

                    for actual_row, expected_row in zip(actual, expected):
                        with self.subTest(period=expected_row["period"]):
                            self.assertEqual(
                                str(actual_row["period"]),
                                str(expected_row["period"]),
                            )
                            self.assertEqual(
                                int(actual_row["transaction_count"]),
                                int(expected_row["transaction_count"]),
                            )
                            self.assertEqual(
                                int(actual_row["amount_count"]),
                                int(expected_row["amount_count"]),
                            )
                            self.assert_number_equal(
                                actual_row["total_amount"],
                                expected_row["total_amount"],
                            )

                            actual_avg = actual_row["average_amount"]
                            expected_avg = expected_row["average_amount"]
                            if expected_avg is None:
                                self.assertTrue(
                                    pd.isna(actual_avg),
                                    f"Expected NULL average for {expected_row['period']}, got {actual_avg}",
                                )
                            else:
                                self.assert_number_equal(
                                    actual_avg, expected_avg
                                )


# ==========================================================
# TD-02: DASHBOARD SUMMARY METRICS
# ==========================================================

    def test_dashboard_summary_matches_sql(self):
        years = self.available_years()
        if not years:
            self.skipTest("No years available.")

        placeholders = ", ".join(
            f":year_{i}" for i in range(len(years))
        )
        params = {f"year_{i}": year for i, year in enumerate(years)}

        expected = self.independent_rows(
            f"""
                SELECT
                    COUNT(*) AS total_count,
                    COUNT(f.amount) AS amount_count,
                    COALESCE(SUM(f.amount), 0) AS total_amount,
                    AVG(f.amount) AS average_amount
                FROM facttransaction f
                JOIN dimdate d ON f.date_key = d.date_key
                WHERE d.year_number IN ({placeholders})
            """,
            params,
        )[0]

        actual = summarize_transaction_trends(
            get_transaction_trends(self.engine, years, "Year")
        )

        self.assertEqual(
            actual["total_count"], int(expected["total_count"])
        )
        self.assert_number_equal(
            actual["total_amount"], expected["total_amount"]
        )

        if expected["amount_count"] == 0:
            self.assertEqual(actual["average_amount"], 0.0)
        else:
            self.assert_number_equal(
                actual["average_amount"], expected["average_amount"]
            )


# ==========================================================
# TD-03: INVALID AND EMPTY INPUTS
# ==========================================================

    def test_empty_year_selection(self):
        result = get_transaction_trends(self.engine, [], "Year")
        self.assertTrue(result.empty)

    def test_invalid_granularity(self):
        with self.assertRaises(ValueError):
            get_transaction_trends(self.engine, [1997], "Invalid")


# ==========================================================
# OLAP-01: TIME ROLL UP
# ==========================================================

    def test_time_rollup(self):
        self.assertEqual(
            len(self.olap_queries), 4,
            "Expected four OLAP queries in the SQL file.",
        )

        rows = self.execute_olap(0)
        self.assertTrue(rows, "Time Roll Up returned no rows.")

        expected = self.independent_rows("""
            SELECT
                COUNT(*) AS trans_count,
                SUM(CASE WHEN t.type = 'Credit' THEN f.amount ELSE 0 END)
                    AS total_credits,
                SUM(CASE WHEN t.type IN ('Debit (Withdrawal)', 'VYBER')
                         THEN f.amount ELSE 0 END) AS total_debits,
                SUM(CASE
                        WHEN t.type = 'Credit' THEN f.amount
                        WHEN t.type IN ('Debit (Withdrawal)', 'VYBER') THEN -f.amount
                        ELSE 0
                    END) AS net_flow
            FROM FactTransaction f
            JOIN DimDate d ON d.date_key = f.date_key
            JOIN DimTransactionType t
                ON t.transaction_type_key = f.transaction_type_key
        """)[0]

        totals = [row for row in rows if str(row["year"]) == "ALL YEARS"]
        self.assertEqual(len(totals), 1, "Expected one grand-total row.")

        for column in ("trans_count", "total_credits", "total_debits", "net_flow"):
            self.assert_number_equal(totals[0][column], expected[column])


# ==========================================================
# OLAP-02: DISTRICT ROLL UP
# ==========================================================

    def test_district_rollup(self):
        rows = self.execute_olap(1)
        self.assertTrue(rows, "District Roll Up returned no rows.")

        expected = self.independent_rows("""
            SELECT
                COUNT(*) AS trans_count,
                COUNT(DISTINCT f.account_key) AS active_accounts,
                ROUND(SUM(f.amount), 2) AS total_volume,
                ROUND(AVG(f.amount), 2) AS avg_trans_amount
            FROM FactTransaction f
            JOIN DimDistrict r ON r.district_key = f.district_key
        """)[0]

        totals = [row for row in rows if str(row["region"]) == "ALL REGIONS"]
        self.assertEqual(len(totals), 1, "Expected one all-regions total row.")

        for column in (
            "trans_count", "active_accounts", "total_volume", "avg_trans_amount"
        ):
            self.assert_number_equal(totals[0][column], expected[column])


# ==========================================================
# OLAP-03: SLICE
# ==========================================================

    def test_slice_1997(self):
        year = 1997
        sql = self.replace_slice_year_filter(self.olap_queries[2], year)
        actual = self.execute_olap(2, sql)
        expected = self.independent_slice_rows(sql, year)
        self.assert_slice_rows_match(actual, expected)

    def test_slice_alternate_year(self):
        years = self.available_years()
        alternate_years = [year for year in years if year != 1997]
        if not alternate_years:
            self.skipTest("No alternate year available for Slice.")

        year = alternate_years[0]
        sql = self.replace_slice_year_filter(self.olap_queries[2], year)
        actual = self.execute_olap(2, sql)
        expected = self.independent_slice_rows(sql, year)
        self.assert_slice_rows_match(actual, expected)


# ==========================================================
# OLAP-04: DICE
# ==========================================================

    def test_dice_default_filters(self):
        rows = self.execute_olap(3)
        self.assertTrue(
            all(
                row["year_number"] in (1996, 1997)
                and row["quarter_number"] in (1, 2)
                and row["region"] in ("Prague", "south Moravia", "north Moravia")
                and row["type"] in ("Credit", "Debit (Withdrawal)")
                and row["frequency"] == "Weekly"
                for row in rows
            ),
            "Dice returned rows outside its filters.",
        )

        expected = self.independent_rows("""
            SELECT
                d.year_number,
                d.quarter_number,
                r.region,
                a.frequency,
                t.type,
                COUNT(*) AS trans_count,
                ROUND(SUM(f.amount), 2) AS total_volume
            FROM FactTransaction f
            JOIN DimDate d ON d.date_key = f.date_key
            JOIN DimDistrict r ON r.district_key = f.district_key
            JOIN DimAccount a ON a.account_key = f.account_key
            JOIN DimTransactionType t
                ON t.transaction_type_key = f.transaction_type_key
            WHERE d.year_number IN (1996, 1997)
              AND d.quarter_number IN (1, 2)
              AND r.region IN ('Prague', 'south Moravia', 'north Moravia')
              AND t.type IN ('Credit', 'Debit (Withdrawal)')
              AND a.frequency = 'Weekly'
            GROUP BY
                d.year_number, d.quarter_number,
                r.region, a.frequency, t.type
            ORDER BY
                d.year_number, d.quarter_number,
                r.region, a.frequency, t.type
        """)

        sort_keys = [
            "year_number", "quarter_number", "region", "frequency", "type"
        ]

        def normalize(records):
            return sorted(
                records,
                key=lambda row: tuple(str(row[key]) for key in sort_keys),
            )

        self.assertEqual(normalize(rows), normalize(expected))


# ==========================================================
# RUN TESTS
# ==========================================================

if __name__ == "__main__":
    unittest.main(verbosity=2)
