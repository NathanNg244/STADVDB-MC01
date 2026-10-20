
import pandas as pd
from sqlalchemy import create_engine

# MySQL connection settings
MYSQL_USER = "root"
MYSQL_PASSWORD = "12345678" # change this to your password 
MYSQL_HOST = "localhost"

# Source database
source_engine = create_engine(
    f"mysql+pymysql://{MYSQL_USER}:{MYSQL_PASSWORD}@{MYSQL_HOST}/financedata"
)

# Target data warehouse
dw_engine = create_engine(
    f"mysql+pymysql://{MYSQL_USER}:{MYSQL_PASSWORD}@{MYSQL_HOST}/financial_dw"
)

# # Test both connections
# with source_engine.connect() as conn:
#     print("Connected to source database: financedata")

# with dw_engine.connect() as conn:
#     print("Connected to data warehouse: financial_dw")



