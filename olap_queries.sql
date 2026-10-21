USE financial_dw;

-- Roll Up
 
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
