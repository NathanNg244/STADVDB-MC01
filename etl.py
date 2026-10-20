
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


