
import streamlit as st
import pandas as pd
import plotly.express as px
from sqlalchemy import create_engine, text
from urllib.parse import quote_plus

# --------------------------------------------------
# 1. PAGE CONFIGURATION
# --------------------------------------------------
st.set_page_config(
    page_title="Financial Dashboard",
    layout="wide"
)

st.title("Financial Data Dashboard")

# --------------------------------------------------
# 2. DATABASE CONNECTION
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
# 3. TEST DATABASE CONNECTION
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
