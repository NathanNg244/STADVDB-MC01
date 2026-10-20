
import pandas as pd
from sqlalchemy import create_engine, text

# ==========================================
# 1. DATABASE CONNECTIONS
# ==========================================

MYSQL_USER = "root"
MYSQL_PASSWORD = "12345678"  # Change to your MySQL password
MYSQL_HOST = "localhost"

# Source database
source_engine = create_engine(
    f"mysql+pymysql://{MYSQL_USER}:{MYSQL_PASSWORD}@{MYSQL_HOST}/financedata"
)

# Target data warehouse
dw_engine = create_engine(
    f"mysql+pymysql://{MYSQL_USER}:{MYSQL_PASSWORD}@{MYSQL_HOST}/financial_dw"
)

# Optional connection tests:
# with source_engine.connect() as conn:
#     print("Connected to financedata")
# with dw_engine.connect() as conn:
#     print("Connected to financial_dw")


# ==========================================
# 2. EXTRACT SOURCE DATA
# ==========================================

print("Extracting source data...")

transactions = pd.read_sql(
    "SELECT * FROM trans", source_engine
)

accounts = pd.read_sql(
    "SELECT account_id, district_id, frequency FROM account",
    source_engine
)

districts = pd.read_sql(
    "SELECT * FROM district", source_engine
)

print(f"Extracted {len(transactions):,} transactions.")
print(f"Extracted {len(accounts):,} accounts.")
print(f"Extracted {len(districts):,} districts.")


# ==========================================
# 3. TRANSFORM DIMENSION DATA
# ==========================================

print("Transforming dimension data...")

# 3.1 DimDate

transactions["newdate"] = pd.to_datetime(
    transactions["newdate"], errors="raise"
)

dim_date = pd.DataFrame({
    "full_date": transactions["newdate"].dt.normalize().drop_duplicates()
})

dim_date["date_key"] = (
    dim_date["full_date"].dt.strftime("%Y%m%d").astype(int)
)
dim_date["day_number"] = dim_date["full_date"].dt.day
dim_date["month_number"] = dim_date["full_date"].dt.month
dim_date["month_name"] = dim_date["full_date"].dt.strftime("%B")
dim_date["quarter_number"] = dim_date["full_date"].dt.quarter
dim_date["year_number"] = dim_date["full_date"].dt.year

dim_date = dim_date[
    [
        "date_key",
        "full_date",
        "day_number",
        "month_number",
        "month_name",
        "quarter_number",
        "year_number"
    ]
].sort_values("full_date").reset_index(drop=True)


# 3.2 DimAccount

dim_account = (
    accounts[["account_id", "frequency"]]
    .drop_duplicates(subset=["account_id"])
    .copy()
)


# 3.3 DimTransactionType

transaction_type_columns = [
    "type",
    "operation",
    "k_symbol",
    "bank",
    "account"
]

dim_transaction_type = (
    transactions[transaction_type_columns]
    .drop_duplicates()
    .copy()
)

# Ensure every transaction has a matching dimension record.
# This avoids SQL joins failing on missing dimension values.
dim_transaction_type = (
    dim_transaction_type.fillna({
        "type": "Unknown",
        "operation": "Unknown",
        "k_symbol": "Unknown",
        "bank": "Unknown",
        "account": "Unknown"
    })
    .drop_duplicates()
    .reset_index(drop=True)
)


# 3.4 DimDistrict

district_columns = [
    "district_id",
    "district_name",
    "region",
    "inhabitants",
    "nocities",
    "ratio_urbaninhabitants",
    "average_salary",
    "unemployment",
    "noentrepreneur",
    "nocrimes"
]

dim_district = (
    districts[district_columns]
    .drop_duplicates(subset=["district_id"])
    .copy()
)


# ==========================================
# 4. LOAD DIMENSION TABLES
# ==========================================

print("Loading dimension tables...")

# IMPORTANT:
# This script expects the DW tables to be empty before a fresh load.
# Do not rerun it against populated tables without clearing the DW
# first, because duplicate primary/unique keys may cause errors.

dim_date.to_sql(
    "dimdate",
    dw_engine,
    if_exists="append",
    index=False,
    chunksize=1000
)
print(f"DimDate loaded: {len(dim_date):,} rows.")

dim_account.to_sql(
    "dimaccount",
    dw_engine,
    if_exists="append",
    index=False,
    chunksize=1000
)
print(f"DimAccount loaded: {len(dim_account):,} rows.")

dim_transaction_type.to_sql(
    "dimtransactiontype",
    dw_engine,
    if_exists="append",
    index=False,
    chunksize=1000
)
print(
    "DimTransactionType loaded:",
    f"{len(dim_transaction_type):,} rows."
)

dim_district.to_sql(
    "dimdistrict",
    dw_engine,
    if_exists="append",
    index=False,
    chunksize=1000
)
print(f"DimDistrict loaded: {len(dim_district):,} rows.")


# ==========================================
# 5. PREPARE FACT TRANSACTION DATA
# ==========================================

print("Preparing fact transaction data...")

# Map each transaction to its date key.

transactions["date_key"] = (
    transactions["newdate"].dt.strftime("%Y%m%d").astype(int)
)

# Replace missing transaction-type attributes consistently
# with the same values used when creating DimTransactionType.

transactions[transaction_type_columns] = (
    transactions[transaction_type_columns].fillna("Unknown")
)

# Get surrogate keys from the loaded dimensions.

with dw_engine.connect() as conn:
    date_lookup = pd.read_sql(
        text("SELECT date_key, full_date FROM dimdate"),
        conn
    )

    account_lookup = pd.read_sql(
        text("SELECT account_key, account_id FROM dimaccount"),
        conn
    )

    type_lookup = pd.read_sql(
        text("""
            SELECT transaction_type_key, type, operation,
                   k_symbol, bank, account
            FROM dimtransactiontype
        """),
        conn
    )

    district_lookup = pd.read_sql(
        text("SELECT district_key, district_id FROM dimdistrict"),
        conn
    )

# Date key lookup

transactions = transactions.merge(
    date_lookup,
    left_on="date_key",
    right_on="date_key",
    how="left",
    validate="many_to_one"
)

# Account key lookup

transactions = transactions.merge(
    account_lookup,
    on="account_id",
    how="left",
    validate="many_to_one"
)

# Transaction type key lookup

transactions = transactions.merge(
    type_lookup,
    on=transaction_type_columns,
    how="left",
    validate="many_to_one"
)

# Get each account's district from the source account table.
account_district = accounts[["account_id", "district_id"]].drop_duplicates(
    subset=["account_id"]
)

transactions = transactions.merge(
    account_district,
    on="account_id",
    how="left",
    validate="many_to_one"
)

# District key lookup

transactions = transactions.merge(
    district_lookup,
    on="district_id",
    how="left",
    validate="many_to_one"
)

# Stop before loading if any required dimension key is missing.

required_keys = [
    "date_key",
    "account_key",
    "transaction_type_key",
    "district_key"
]

missing_keys = transactions[required_keys].isna().sum()

if missing_keys.any():
    raise ValueError(
        "Some transactions could not be mapped to dimension keys:\n"
        + missing_keys[missing_keys > 0].to_string()
        + "\nCheck the source data and dimension tables before proceeding."
    )

# Build the fact table using the columns in the existing schema.

fact_transaction = transactions[
    [
        "trans_id",
        "date_key",
        "account_key",
        "transaction_type_key",
        "district_key",
        "amount",
        "balance"
    ]
].copy()

fact_transaction["amount"] = pd.to_numeric(
    fact_transaction["amount"], errors="raise"
)
fact_transaction["balance"] = pd.to_numeric(
    fact_transaction["balance"], errors="raise"
)

# Ensure the date key is represented by the date dimension's key.
# Remove the extra date column introduced by the date merge.
transactions.drop(columns=["full_date"], inplace=True, errors="ignore")


# ==========================================
# 6. LOAD FACT TRANSACTION TABLE
# ==========================================

print("Loading fact transaction table...")

fact_transaction.to_sql(
    "facttransaction",
    dw_engine,
    if_exists="append",
    index=False,
    chunksize=5000
)

print(
    f"FactTransaction loaded: {len(fact_transaction):,} rows."
)


# ==========================================
# 7. FINAL ROW COUNTS
# ==========================================

print("\nETL completed. Final warehouse row counts:")

with dw_engine.connect() as conn:
    for table in [
        "dimdate",
        "dimaccount",
        "dimtransactiontype",
        "dimdistrict",
        "facttransaction"
    ]:
        count = conn.execute(
            text(f"SELECT COUNT(*) FROM {table}")
        ).scalar_one()

        print(f"{table}: {count:,} rows")


# Close database connections.

source_engine.dispose()
dw_engine.dispose()

print("\nETL process finished successfully.")
