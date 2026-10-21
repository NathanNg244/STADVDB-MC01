
import streamlit as st
import pandas as pd
import plotly.express as px
from sqlalchemy import create_engine, text
from urllib.parse import quote_plus

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
# Update these values to match your local MySQL setup.
DB_USER = "root"
DB_PASSWORD = "12345678"
DB_HOST = "localhost"
DB_PORT = 3306
DB_NAME = "financial_dw"


@st.cache_resource
def get_engine():
    connection_url = (
        f"mysql+pymysql://{quote_plus(DB_USER)}:"
        f"{quote_plus(DB_PASSWORD)}@{DB_HOST}:"
        f"{DB_PORT}/{DB_NAME}"
    )
    return create_engine(
        connection_url,
        pool_pre_ping=True
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
        "Check your MySQL username, password, port, "
        "and that MySQL Server is running."
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

    # Select the appropriate date hierarchy level
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

    else:
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

    # Create parameter placeholders for the selected years
    year_placeholders = ", ".join(
        f":year_{i}" for i in range(len(selected_years))
    )

    trend_query = f"""
    SELECT
        {period_expression} AS period,
        COUNT(*) AS transaction_count,
        COALESCE(SUM(f.amount), 0) AS total_amount,
        AVG(f.amount) AS average_amount
    FROM facttransaction f
    JOIN dimdate d
        ON f.date_key = d.date_key
    WHERE d.year_number IN ({year_placeholders})
    GROUP BY {group_columns}
    ORDER BY {order_columns}
    """

    query_parameters = {
        f"year_{i}": year
        for i, year in enumerate(selected_years)
    }

    trend_df = pd.read_sql(
        text(trend_query),
        engine,
        params=query_parameters
    )

    if trend_df.empty:
        st.warning("No transactions match your selections.")

    else:
        # Summary metrics for the filtered transactions
        total_count = int(trend_df["transaction_count"].sum())
        total_amount = trend_df["total_amount"].sum()

        # Weighted overall average across all selected periods
        overall_average = (
            total_amount / total_count if total_count else 0
        )

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

        display_df = trend_df.rename(
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
#  OTHER QUERIES 
# --------------------------------------------------