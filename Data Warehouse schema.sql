
CREATE DATABASE IF NOT EXISTS financial_dw;

USE financial_dw;

-- -------------------------------------------------------
-- Dimension Tables
-- -------------------------------------------------------

CREATE TABLE DimDate (
    date_key INT PRIMARY KEY,
    full_date DATE NOT NULL UNIQUE,
    day_number INT NOT NULL,
    month_number INT NOT NULL,
    month_name VARCHAR(15) NOT NULL,
    quarter_number INT NOT NULL,
    year_number INT NOT NULL
);

CREATE TABLE DimAccount (
    account_key INT AUTO_INCREMENT PRIMARY KEY,
    account_id INT NOT NULL UNIQUE,
    frequency VARCHAR(50)
);

CREATE TABLE DimTransactionType (
    transaction_type_key INT AUTO_INCREMENT PRIMARY KEY,
    type VARCHAR(50),
    operation VARCHAR(100),
    k_symbol VARCHAR(100),
    bank VARCHAR(100),
    account VARCHAR(50)
);

CREATE TABLE DimDistrict (
    district_key INT AUTO_INCREMENT PRIMARY KEY,
    district_id INT NOT NULL UNIQUE,
    district_name VARCHAR(100),
    region VARCHAR(100),
    inhabitants INT,
    average_salary DECIMAL(15,2),
    unemployment DOUBLE
);

-- -------------------------------------------------------
-- Fact Table
-- -------------------------------------------------------

CREATE TABLE FactTransaction (
    transaction_key INT AUTO_INCREMENT PRIMARY KEY,
    trans_id INT NOT NULL UNIQUE,
    date_key INT NOT NULL,
    account_key INT NOT NULL,
    transaction_type_key INT NOT NULL,
    district_key INT NOT NULL,
    amount DECIMAL(15,2),
    balance DECIMAL(15,2),

    CONSTRAINT fk_fact_date
        FOREIGN KEY (date_key)
        REFERENCES DimDate(date_key),

    CONSTRAINT fk_fact_account
        FOREIGN KEY (account_key)
        REFERENCES DimAccount(account_key),

    CONSTRAINT fk_fact_transaction_type
        FOREIGN KEY (transaction_type_key)
        REFERENCES DimTransactionType(transaction_type_key),

    CONSTRAINT fk_fact_district
        FOREIGN KEY (district_key)
        REFERENCES DimDistrict(district_key)
);


