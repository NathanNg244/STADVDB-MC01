USE financial_dw;

-- Roll Up (Time Hierarchy: Year > Quarter > Month)

WITH TypeAgg AS (
    SELECT
        date_key,
        transaction_type_key,
        COUNT(*) AS trans_count,
        SUM(amount) AS amount_sum
    FROM FactTransaction
    GROUP BY date_key, transaction_type_key
),
DateAgg AS (
    SELECT
        f.date_key,
        SUM(f.trans_count) AS trans_count,
        SUM(CASE WHEN t.type = 'Credit' THEN f.amount_sum ELSE 0 END) AS total_credits,
        SUM(CASE WHEN t.type IN ('Debit (Withdrawal)', 'VYBER') THEN f.amount_sum ELSE 0 END) AS total_debits,
        SUM(CASE WHEN t.type = 'Credit' THEN f.amount_sum
                 WHEN t.type IN ('Debit (Withdrawal)', 'VYBER') THEN -f.amount_sum ELSE 0 END) AS net_flow
    FROM TypeAgg AS f
    JOIN DimTransactionType t ON t.transaction_type_key = f.transaction_type_key
    GROUP BY f.date_key
)
SELECT
    IF(GROUPING(d.year_number)    = 1, 'ALL YEARS',    CAST(d.year_number AS CHAR))    AS year,
    IF(GROUPING(d.quarter_number) = 1, 'ALL QUARTERS', CONCAT('Q', d.quarter_number))  AS quarter,
    IF(GROUPING(d.month_number)   = 1, 'ALL MONTHS',   MAX(d.month_name))              AS month,
    SUM(f.trans_count) AS trans_count,
    SUM(f.total_credits) AS total_credits,
    SUM(f.total_debits) AS total_debits,
    SUM(f.net_flow) AS net_flow
FROM DateAgg f
JOIN DimDate d ON d.date_key = f.date_key
GROUP BY d.year_number, d.quarter_number, d.month_number WITH ROLLUP
ORDER BY GROUPING(d.year_number),    d.year_number,
         GROUPING(d.quarter_number), d.quarter_number,
         GROUPING(d.month_number),   d.month_number;

-- Roll Up (District Hierarchy: Region > District)

WITH AccountDistrictAgg AS (
    SELECT
        district_key,
        account_key,
        COUNT(*) AS trans_count,
        COUNT(amount) AS amount_count,
        SUM(amount) AS amount_sum
    FROM FactTransaction
    GROUP BY district_key, account_key
)
SELECT
    IF(GROUPING(r.region)        = 1, 'ALL REGIONS',   r.region)        AS region,
    IF(GROUPING(r.district_name) = 1, 'ALL DISTRICTS', r.district_name) AS district,
    SUM(f.trans_count)                                 AS trans_count,
    COUNT(DISTINCT f.account_key)            AS active_accounts,
    ROUND(SUM(f.amount_sum), 2)                  AS total_volume,
    ROUND(SUM(f.amount_sum) / NULLIF(SUM(f.amount_count), 0), 2)                  AS avg_trans_amount
FROM AccountDistrictAgg f
JOIN DimDistrict r ON r.district_key = f.district_key
GROUP BY r.region, r.district_name WITH ROLLUP
ORDER BY GROUPING(r.region), r.region,
         GROUPING(r.district_name), total_volume DESC;

-- Slice (Year = 1997)

WITH FactAgg AS (
    SELECT
        f.district_key,
        f.transaction_type_key,
        COUNT(*) AS trans_count,
        COUNT(f.amount) AS amount_count,
        SUM(f.amount) AS amount_sum,
        MAX(f.amount) AS largest_trans
    FROM FactTransaction f
    JOIN DimDate d ON d.date_key = f.date_key
    WHERE d.year_number = 1997
    GROUP BY f.district_key, f.transaction_type_key
)
SELECT r.region,
       t.type,
       SUM(a.trans_count)                          AS trans_count,
       ROUND(SUM(a.amount_sum), 2)           AS total_volume,
       ROUND(SUM(a.amount_sum) / NULLIF(SUM(a.amount_count), 0), 2)           AS avg_amount,
       ROUND(MAX(a.largest_trans), 2)           AS largest_trans
FROM FactAgg a
JOIN DimDistrict        r ON r.district_key = a.district_key
JOIN DimTransactionType t ON t.transaction_type_key = a.transaction_type_key
GROUP BY r.region, t.type
HAVING COUNT(a.trans_count) >= 100
ORDER BY r.region, total_volume DESC;

-- Dice (Time: 1996-1997; Quarter: 1, 2; Type: Credit, Debit; Region: Prague, South Moravia, North Moravia; Frequency: Weekly)

WITH weekly_accounts AS (
    SELECT account_key, frequency
    FROM DimAccount
    WHERE frequency = 'Weekly'
)
SELECT STRAIGHT_JOIN
       d.year_number,
       d.quarter_number,
       r.region,
       a.frequency,
       t.type,
       COUNT(*)                AS trans_count,
       ROUND(SUM(f.amount), 2) AS total_volume
FROM weekly_accounts a
JOIN FactTransaction   f ON f.account_key = a.account_key
JOIN DimDate            d ON d.date_key = f.date_key
JOIN DimDistrict        r ON r.district_key = f.district_key
JOIN DimTransactionType t ON t.transaction_type_key = f.transaction_type_key
WHERE d.year_number    IN (1996, 1997)                             
  AND d.quarter_number IN (1, 2)
  AND r.region         IN ('Prague', 'south Moravia', 'north Moravia') 
  AND t.type           IN ('Credit', 'Debit (Withdrawal)')
GROUP BY d.year_number, d.quarter_number, r.region, a.frequency, t.type
ORDER BY d.year_number, d.quarter_number, r.region, total_volume DESC;
