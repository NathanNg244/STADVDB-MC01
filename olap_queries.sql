USE financial_dw;

-- Roll Up (Time Hierarchy: Year > Quarter > Month)
 
SELECT
    IF(GROUPING(d.year_number)    = 1, 'ALL YEARS',    CAST(d.year_number AS CHAR))    AS year,
    IF(GROUPING(d.quarter_number) = 1, 'ALL QUARTERS', CONCAT('Q', d.quarter_number))  AS quarter,
    IF(GROUPING(d.month_number)   = 1, 'ALL MONTHS',   MAX(d.month_name))              AS month,
    COUNT(*)                                                                           AS trans_count,
    SUM(CASE WHEN t.type = 'Credit' THEN f.amount ELSE 0 END)                          AS total_credits,
    SUM(CASE WHEN t.type IN ('Debit (Withdrawal)','VYBER') THEN f.amount ELSE 0 END)                AS total_debits,
    SUM(CASE WHEN t.type = 'Credit' THEN f.amount
             WHEN t.type IN ('Debit (Withdrawal)','VYBER') THEN -f.amount ELSE 0 END)               AS net_flow
FROM FactTransaction f
JOIN DimDate            d ON d.date_key = f.date_key
JOIN DimTransactionType t ON t.transaction_type_key = f.transaction_type_key
GROUP BY d.year_number, d.quarter_number, d.month_number WITH ROLLUP
ORDER BY GROUPING(d.year_number),    d.year_number,
         GROUPING(d.quarter_number), d.quarter_number,
         GROUPING(d.month_number),   d.month_number;

-- Roll Up (District Hierarchy: Region > District)

SELECT
    IF(GROUPING(r.region)        = 1, 'ALL REGIONS',   r.region)        AS region,
    IF(GROUPING(r.district_name) = 1, 'ALL DISTRICTS', r.district_name) AS district,
    COUNT(*)                                 AS trans_count,
    COUNT(DISTINCT f.account_key)            AS active_accounts,
    ROUND(SUM(f.amount), 2)                  AS total_volume,
    ROUND(AVG(f.amount), 2)                  AS avg_trans_amount
FROM FactTransaction f
JOIN DimDistrict r ON r.district_key = f.district_key
GROUP BY r.region, r.district_name WITH ROLLUP
ORDER BY GROUPING(r.region), r.region,
         GROUPING(r.district_name), total_volume DESC;

-- Slice (Year = 1997)

SELECT r.region,
       t.type,
       COUNT(*)                          AS trans_count,
       ROUND(SUM(f.amount), 2)           AS total_volume,
       ROUND(AVG(f.amount), 2)           AS avg_amount,
       ROUND(MAX(f.amount), 2)           AS largest_trans
FROM FactTransaction f
JOIN DimDate            d ON d.date_key = f.date_key
JOIN DimDistrict        r ON r.district_key = f.district_key
JOIN DimTransactionType t ON t.transaction_type_key = f.transaction_type_key
WHERE d.year_number = 1997                       
GROUP BY r.region, t.type
HAVING COUNT(*) >= 100
ORDER BY r.region, total_volume DESC;
