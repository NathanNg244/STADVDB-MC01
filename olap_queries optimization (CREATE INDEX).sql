USE financial_dw;

-- Roll Up (Time Hierarchy: Year > Quarter > Month)

CREATE INDEX idx_fact_date_type_amount
ON FactTransaction (date_key, transaction_type_key, amount);

-- Roll Up (District Hierarchy: Region > District)

CREATE INDEX idx_fact_district_account_amount
ON FactTransaction (district_key, account_key, amount);

-- Slice (Year = 1997)

CREATE INDEX idx_fact_date_district_type_amount
ON FactTransaction (date_key, district_key, transaction_type_key, amount);

-- Dice (Time: 1996-1997; Quarter: 1, 2; Type: Credit, Debit; Region: Prague, South Moravia, North Moravia; Frequency: Weekly)

CREATE INDEX idx_account_frequency_key
ON DimAccount (frequency, account_key);

CREATE INDEX idx_fact_account_date_district_type_amount
ON FactTransaction (account_key, date_key, district_key, transaction_type_key, amount);