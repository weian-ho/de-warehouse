-- T025: does the Python loader (raw.agg_trades_py) reproduce the bash loader (raw.agg_trades)?

-- 1) Verdict per file and batch: row counts must be identical.
WITH b AS (SELECT symbol, source_file, batch_id, count(*) AS n FROM raw.agg_trades    GROUP BY 1, 2, 3),
     p AS (SELECT symbol, source_file, batch_id, count(*) AS n FROM raw.agg_trades_py GROUP BY 1, 2, 3)
SELECT coalesce(b.symbol, p.symbol)           AS symbol,
       coalesce(b.source_file, p.source_file) AS source_file,
       coalesce(b.batch_id, p.batch_id)       AS batch_id,
       b.n AS bash_rows,
       p.n AS py_rows,
       CASE WHEN b.n = p.n THEN 'MATCH' ELSE 'MISMATCH' END AS verdict
FROM b FULL OUTER JOIN p USING (symbol, source_file, batch_id)
ORDER BY 1, 2, 3;

-- 2) Content fingerprint per file and batch: one cheap pass over each table.
--    Each column is summarised; if any single value differed, its fingerprint would differ.
--    (Replaces a row-by-row EXCEPT ALL, which needs all rows in memory at once and
--     crashed Postgres on the full dataset.)
WITH f AS (
    SELECT 'bash' AS side, symbol, source_file, batch_id,
           count(*)                      AS n,
           sum(agg_trade_id)             AS s_id,
           sum(price)                    AS s_price,
           sum(qty)                      AS s_qty,
           sum(first_trade_id)           AS s_first,
           sum(last_trade_id)            AS s_last,
           sum(ts_raw)                   AS s_ts,
           count(*) FILTER (WHERE is_buyer_maker) AS n_maker,
           count(*) FILTER (WHERE is_best_match)  AS n_best,
           count(*) - count(first_trade_id) + count(*) - count(last_trade_id)
             + count(*) - count(is_buyer_maker) + count(*) - count(is_best_match) AS n_nulls
    FROM raw.agg_trades GROUP BY symbol, source_file, batch_id
    UNION ALL
    SELECT 'python', symbol, source_file, batch_id,
           count(*), sum(agg_trade_id), sum(price), sum(qty), sum(first_trade_id), sum(last_trade_id),
           sum(ts_raw),
           count(*) FILTER (WHERE is_buyer_maker),
           count(*) FILTER (WHERE is_best_match),
           count(*) - count(first_trade_id) + count(*) - count(last_trade_id)
             + count(*) - count(is_buyer_maker) + count(*) - count(is_best_match)
    FROM raw.agg_trades_py GROUP BY symbol, source_file, batch_id
)
SELECT symbol, source_file, batch_id,
       CASE WHEN count(DISTINCT (n, s_id, s_price, s_qty, s_first, s_last, s_ts, n_maker, n_best, n_nulls)) = 1
             AND count(*) = 2
            THEN 'MATCH' ELSE 'MISMATCH' END AS content_verdict
FROM f
GROUP BY symbol, source_file, batch_id
ORDER BY 1, 2, 3;