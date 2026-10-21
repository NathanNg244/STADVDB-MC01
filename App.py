
import streamlit as st
import pandas as pd
import plotly.express as px
from sqlalchemy import create_engine, text
from urllib.parse import quote_plus

import io
import unittest
import subprocess
import sys
from pathlib import Path
from contextlib import redirect_stdout, redirect_stderr
from test_dashboard import get_transaction_trends, summarize_transaction_trends

# --------------------------------------------------
# PAGE CONFIGURATION
# --------------------------------------------------
st.set_page_config(
    page_title="Financial Dashboard",
    layout="wide"
)

st.title("Financial Data Dashboard")

# --------------------------------------------------
# DATABASE CONNECTION
# --------------------------------------------------
@st.cache_resource
def get_engine():
    db = st.secrets["mysql"]

    connection_url = (
        f"mysql+pymysql://{quote_plus(db['user'])}:"
        f"{quote_plus(db['password'])}@{db['host']}:"
        f"{db['port']}/{db['database']}"
    )

    return create_engine(
        connection_url,
        pool_pre_ping=True,
        connect_args={"ssl": {}}
    )

# --------------------------------------------------
# TEST DATABASE CONNECTION
# --------------------------------------------------
try:
    engine = get_engine()

    with engine.connect() as connection:
        total_transactions = connection.execute(
            text("SELECT COUNT(*) FROM facttransaction")
        ).scalar_one()

    st.success("Connected to financial_dw successfully!")

except Exception as error:
    st.error("Could not connect to the MySQL database.")
    st.code(str(error))
    st.info(
        "Check your Aiven connection details, Streamlit Secrets, "
        "and database availability."
    )
    st.stop()


# --------------------------------------------------
# QUERY 1 - INTERACTIVE TRANSACTION TRENDS
# --------------------------------------------------
st.divider()
st.header("Transaction Trends Over Time")

# Get the years available in the warehouse
years_query = """
SELECT DISTINCT year_number
FROM dimdate
ORDER BY year_number
"""

try:
    years_df = pd.read_sql(text(years_query), engine)

    if years_df.empty:
        st.warning("No years were found")
        st.stop()

    available_years = (
        years_df["year_number"].astype(int).tolist()
    )

    # Dashboard filters
    filter_col1, filter_col2 = st.columns(2)

    with filter_col1:
        selected_years = st.multiselect(
            "Select year(s) to analyze",
            options=available_years,
            default=available_years,
            key="trend_years"
        )

    with filter_col2:
        granularity = st.selectbox(
            "Time granularity",
            options=["Year", "Quarter", "Month"],
            index=0,
            key="trend_granularity"
        )

    if not selected_years:
        st.info("Select at least one year to view the report.")
        st.stop()

    trend_df = get_transaction_trends(
        engine=engine,
        selected_years=selected_years,
        granularity=granularity,
    )

    if trend_df.empty:
        st.warning("No transactions match your selections.")

    else:
        # Summary metrics for the filtered transactions
        summary = summarize_transaction_trends(trend_df)

        total_count = summary["total_count"]
        total_amount = summary["total_amount"]
        overall_average = summary["average_amount"]

        metric1, metric2, metric3 = st.columns(3)

        metric1.metric(
            "Total Transactions",
            f"{total_count:,}"
        )

        metric2.metric(
            "Total Transaction Amount",
            f"{total_amount:,.2f}"
        )

        metric3.metric(
            "Average Transaction Amount",
            f"{overall_average:,.2f}"
        )

        # Choose what to display in the chart
        chart_metric = st.radio(
            "Chart metric",
            options=[
                "Transaction Count",
                "Total Amount",
                "Average Amount"
            ],
            horizontal=True,
            key="trend_metric"
        )

        metric_columns = {
            "Transaction Count": "transaction_count",
            "Total Amount": "total_amount",
            "Average Amount": "average_amount"
        }

        selected_metric = metric_columns[chart_metric]

        # Display chart
        st.subheader(
            f"{chart_metric} by {granularity.lower()}"
        )

        figure = px.bar(
            trend_df,
            x="period",
            y=selected_metric,
            title=f"{chart_metric} by {granularity}",
            labels={
                "period": "Time Period",
                selected_metric: chart_metric
            },
            text_auto=".2s"
        )

        figure.update_layout(
            xaxis_title="Time Period",
            yaxis_title=chart_metric,
            xaxis_tickangle=-45
        )

        st.plotly_chart(
            figure,
            use_container_width=True
        )

        # Display detailed aggregated results

        display_df = trend_df.drop(columns=["amount_count"]).rename(
            columns={
                "period": "Period",
                "transaction_count": "Transaction Count",
                "total_amount": "Total Amount",
                "average_amount": "Average Amount"
            }
        )

        st.dataframe(
            display_df,
            use_container_width=True,
            hide_index=True
        )

        # Allow users to export the report
        st.download_button(
            "Download report as CSV",
            data=display_df.to_csv(index=False).encode("utf-8"),
            file_name="transaction_trends.csv",
            mime="text/csv"
        )


except Exception as error:
    st.error("An error occurred while generating Transaction Trends.")
    st.code(str(error))


# --------------------------------------------------
# OLAP TESTING
# --------------------------------------------------

st.divider()
st.header("OLAP Testing")

st.write(
    "Run functional tests to verify correctness, "
    "or performance tests to measure OLAP query execution time."
)

# Project paths
PROJECT_DIR = Path(__file__).resolve().parent
FUNCTIONAL_TEST_FILE = PROJECT_DIR / "test_dashboard.py"
PERFORMANCE_TEST_FILE = PROJECT_DIR / "performance_test.py"
RESULTS_DIR = PROJECT_DIR / "results"

# --------------------------------------------------
# TEST OPTIONS
# --------------------------------------------------

st.subheader("Test Configuration")

test_col1, test_col2 = st.columns(2)

with test_col1:
    performance_runs = st.number_input(
        "Performance test repetitions",
        min_value=1,
        max_value=50,
        value=5,
        step=1,
        key="performance_runs",
    )

with test_col2:
    performance_warmups = st.number_input(
        "Warm-up repetitions",
        min_value=0,
        max_value=10,
        value=1,
        step=1,
        key="performance_warmups",
    )

explain_enabled = st.checkbox(
    "Collect SQL EXPLAIN plans",
    value=False,
    key="performance_explain",
)

analyze_enabled = st.checkbox(
    "Use EXPLAIN ANALYZE (executes SQL)",
    value=False,
    key="performance_analyze",
    help=(
        "Requires a compatible MySQL version. "
        "This executes the analyzed SQL and may take additional time."
    ),
)

if analyze_enabled:
    explain_enabled = True

st.caption(
    "Functional Testing checks correctness. Performance Testing "
    "measures query execution times and can collect SQL execution plans."
)

# --------------------------------------------------
# TWO TEST BUTTONS
# --------------------------------------------------

functional_col, performance_col = st.columns(2)

with functional_col:
    run_functional = st.button(
        "Run Functional Testing",
        type="primary",
        use_container_width=True,
        key="run_functional_testing",
    )

with performance_col:
    run_performance = st.button(
        "Run Performance Testing",
        type="primary",
        use_container_width=True,
        key="run_performance_testing",
    )

# --------------------------------------------------
# FUNCTIONAL TESTING
# --------------------------------------------------

if run_functional:
    st.subheader("Functional Testing Results")

    if not FUNCTIONAL_TEST_FILE.is_file():
        st.error(
            f"Test file not found: {FUNCTIONAL_TEST_FILE}"
        )
    else:
        command = [
            sys.executable,
            "-m",
            "unittest",
            "-v",
            "test_dashboard",
        ]

        try:
            with st.spinner(
                "Running functional tests..."
            ):
                completed = subprocess.run(
                    command,
                    cwd=str(PROJECT_DIR),
                    capture_output=True,
                    text=True,
                    timeout=600,
                    check=False,
                )

            test_output = (
                completed.stdout + "\n" + completed.stderr
            ).strip()

            st.code(
                test_output or "No test output.",
                language="text",
            )

            if completed.returncode == 0:
                st.success(
                    "Functional test process completed "
                    "without reported failures."
                )
            else:
                st.error(
                    "Functional testing failed or could "
                    "not complete. Review the output above."
                )

        except subprocess.TimeoutExpired:
            st.error(
                "Functional testing timed out after 10 minutes."
            )

        except Exception as error:
            st.error("Could not run functional testing.")
            st.exception(error)


# --------------------------------------------------
# PERFORMANCE TESTING
# --------------------------------------------------

if run_performance:
    st.subheader("Performance Testing Results")

    if not PERFORMANCE_TEST_FILE.is_file():
        st.error(
            f"Performance script not found: "
            f"{PERFORMANCE_TEST_FILE}"
        )
    else:
        performance_label = st.selectbox(
            "Benchmark label",
            options=["before", "after"],
            index=0,
            key="performance_label",
            help=(
                "Use before for measurements before optimization "
                "and after for measurements after optimization."
            ),
        )

        command = [
            sys.executable,
            str(PERFORMANCE_TEST_FILE),
            "--label",
            performance_label,
            "--runs",
            str(int(performance_runs)),
            "--warmups",
            str(int(performance_warmups)),
            "--output",
            str(RESULTS_DIR),
        ]

        if explain_enabled:
            command.append("--explain")

        if analyze_enabled:
            command.append("--analyze")

        try:
            with st.spinner(
                "Running performance benchmarks. "
                "This may take several minutes..."
            ):
                completed = subprocess.run(
                    command,
                    cwd=str(PROJECT_DIR),
                    capture_output=True,
                    text=True,
                    timeout=1800,
                    check=False,
                )

            performance_output = (
                completed.stdout + "\n" + completed.stderr
            ).strip()

            st.code(
                performance_output or "No benchmark output.",
                language="text",
            )

            if completed.returncode == 0:
                st.success(
                    "Performance testing completed."
                )

                # Show available result files.
                report_file = (
                    RESULTS_DIR / f"{performance_label}.json"
                )
                csv_file = (
                    RESULTS_DIR / f"{performance_label}.csv"
                )
                explain_file = (
                    RESULTS_DIR
                    / f"{performance_label}_explain.txt"
                )

                if report_file.is_file():
                    st.download_button(
                        "Download JSON report",
                        data=report_file.read_bytes(),
                        file_name=report_file.name,
                        mime="application/json",
                        key=f"download_json_{performance_label}",
                    )

                if csv_file.is_file():
                    st.download_button(
                        "Download CSV measurements",
                        data=csv_file.read_bytes(),
                        file_name=csv_file.name,
                        mime="text/csv",
                        key=f"download_csv_{performance_label}",
                    )

                if explain_enabled and explain_file.is_file():
                    st.download_button(
                        "Download SQL execution plans",
                        data=explain_file.read_bytes(),
                        file_name=explain_file.name,
                        mime="text/plain",
                        key=f"download_explain_{performance_label}",
                    )

            else:
                st.error(
                    "Performance testing failed. "
                    "Review the error output above."
                )

        except subprocess.TimeoutExpired:
            st.error(
                "Performance testing timed out after "
                "30 minutes."
            )

        except Exception as error:
            st.error("Could not run performance testing.")
            st.exception(error)


# --------------------------------------------------
#  OTHER QUERIES 
# --------------------------------------------------
