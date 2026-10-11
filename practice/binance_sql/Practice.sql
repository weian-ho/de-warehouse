-- The 15 questions

-- Each carries a check — a way to prove your answer without trusting it.

-- Dedupe

-- 1.Which (symbol, agg_trade_id) pairs appear more than once, and which batches produced them? Check: exactly the row count of the re-loaded file.
SELECT
    symbol,
    agg_trade_id
FROM raw.agg_trades
GROUP BY symbol, agg_trade_id
HAVING COUNT(*) > 1;
-- 2.Deduplicate keeping the latest-loaded row, using ROW_NUMBER(). Check: equals count(DISTINCT (symbol, agg_trade_id)).
WITH dedu AS (
    SELECT
        symbol,
        agg_trade_id,
        price,
        qty,
        first_trade_id,
        last_trade_id,
        ts_raw,
        is_buyer_maker,
        is_best_match,
        source_file,
        batch_id,
        loaded_at
    FROM (
        SELECT *,
               ROW_NUMBER() OVER (
                   PARTITION BY symbol, agg_trade_id
                   ORDER BY loaded_at DESC
               ) AS rnk
        FROM raw.agg_trades
    ) t
    WHERE rnk = 1
)

SELECT *
FROM dedu;
-- 3.Same result with Postgres's DISTINCT ON. Check: EXCEPT both directions returns zero rows.
SELECT DISTINCT ON (symbol, agg_trade_id)
       *
FROM raw.agg_trades
ORDER BY
    symbol,
    agg_trade_id,
    loaded_at DESC;
-- Gaps and islands
-- 4. Missing 1-minute candles per symbol, using a generate_series minute spine. Check: spine rows minus candle rows.
WITH bounds AS (
    SELECT
        symbol,
        open_time_raw,
        CASE
            WHEN open_time_raw >= 100000000000000
                THEN TIMESTAMPTZ 'epoch'
                     + open_time_raw * INTERVAL '1 microsecond'
            ELSE TIMESTAMPTZ 'epoch'
                 + open_time_raw * INTERVAL '1 millisecond'
        END AS open_time
    FROM raw.klines_1m
),

series AS (
    SELECT
        symbol,
        generate_series(
            TIMESTAMPTZ '2024-12-30 00:00:00+00',
            TIMESTAMPTZ '2025-01-02 23:59:00+00',
            INTERVAL '1 minute'
        ) AS ktime
    FROM raw.klines_1m
    GROUP BY symbol
)

SELECT
    m.symbol,
    m.ktime
FROM series m
LEFT JOIN bounds b
    ON m.ktime = b.open_time
   AND m.symbol = b.symbol
WHERE b.open_time IS NULL;
-- 5. Collapse missing minutes into contiguous outage ranges with start, end and length. Check: lengths sum to Q4.
WITH bounds AS (
    SELECT
        symbol,
        CASE
            WHEN open_time_raw >= 100000000000000
                THEN TIMESTAMPTZ 'epoch'
                     + open_time_raw * INTERVAL '1 microsecond'
            ELSE TIMESTAMPTZ 'epoch'
                 + open_time_raw * INTERVAL '1 millisecond'
        END AS open_time
    FROM raw.klines_1m
),

series AS (
    SELECT
        symbol,
        generate_series(
            TIMESTAMPTZ '2024-12-30 00:00:00+00',
            TIMESTAMPTZ '2025-01-02 23:59:00+00',
            INTERVAL '1 minute'
        ) AS ktime
    FROM raw.klines_1m
    GROUP BY symbol
),

missing AS (
    SELECT
        m.symbol,
        m.ktime
    FROM series m
    LEFT JOIN bounds b
        ON m.ktime = b.open_time
       AND m.symbol = b.symbol
    WHERE b.open_time IS NULL
),

previous AS (
    SELECT
        symbol,
        ktime,
        LAG(ktime) OVER (
            PARTITION BY symbol
            ORDER BY ktime
        ) AS ptime
    FROM missing
),

flaging AS (
    SELECT
        symbol,
        ktime,
        ptime,
        CASE
            WHEN ktime - ptime > INTERVAL '1 minute'
                THEN 1
            ELSE 0
        END AS flag
    FROM previous
),

island AS (
    SELECT
        symbol,
        ktime,
        SUM(flag) OVER (
            PARTITION BY symbol
            ORDER BY ktime
        ) AS outage_id
    FROM flaging
)

SELECT
    symbol,
    outage_id,
    MIN(ktime) AS starttime,
    MAX(ktime) AS endtime,
    COUNT(*) AS length
FROM island
GROUP BY symbol, outage_id;
-- 6. Longest run of consecutive up-candles (close > open) per symbol, with its start and end.

-- 7. Stretches of five or more consecutive minutes where volume exceeded that symbol-day's 90th percentile.

-- Sessionisation
-- 8. Assign a burst_id to each trade: a new burst starts when the gap to the previous trade in the same symbol exceeds 2 seconds.
WITH dedu AS (
    SELECT
        symbol,
        agg_trade_id,
        price,
        qty,
        first_trade_id,
        last_trade_id,
        ts_raw,
        is_buyer_maker,
        is_best_match,
        source_file,
        batch_id,
        loaded_at
    FROM (
        SELECT *,
               ROW_NUMBER() OVER (
                   PARTITION BY symbol, agg_trade_id
                   ORDER BY loaded_at DESC
               ) AS rnk
        FROM raw.agg_trades
    ) t
    WHERE rnk = 1
),

bounds AS (
    SELECT
        symbol,
        agg_trade_id,
        price,
        qty,
        CASE
            WHEN ts_raw >= 100000000000000
                THEN TIMESTAMPTZ 'epoch'
                     + ts_raw * INTERVAL '1 microsecond'
            ELSE TIMESTAMPTZ 'epoch'
                 + ts_raw * INTERVAL '1 millisecond'
        END AS trade_time
    FROM dedu
),

previous AS (
    SELECT
        symbol,
        agg_trade_id,
        price,
        qty,
        trade_time,
        LAG(trade_time) OVER (
            PARTITION BY symbol
            ORDER BY trade_time, agg_trade_id
        ) AS previous_time
    FROM bounds
),

threshold AS (
    SELECT
        *,
        CASE
            WHEN trade_time - previous_time > INTERVAL '2 seconds'
                THEN 1
            ELSE 0
        END AS thres
    FROM previous
),

summing AS (
    SELECT
        *,
        SUM(thres) OVER (
            PARTITION BY symbol
            ORDER BY trade_time, agg_trade_id
        ) AS burst_id
    FROM threshold
)

SELECT *
FROM summing;
-- 9. Per burst: start, end, duration, trade count, volume, VWAP (sum(price*qty)/sum(qty)), price change.
WITH dedu AS (
    SELECT
        symbol,
        agg_trade_id,
        price,
        qty,
        first_trade_id,
        last_trade_id,
        ts_raw,
        is_buyer_maker,
        is_best_match,
        source_file,
        batch_id,
        loaded_at
    FROM (
        SELECT *,
               ROW_NUMBER() OVER (
                   PARTITION BY symbol, agg_trade_id
                   ORDER BY loaded_at DESC
               ) AS rnk
        FROM raw.agg_trades
    ) t
    WHERE rnk = 1
),

bounds AS (
    SELECT
        symbol,
        agg_trade_id,
        price,
        qty,
        first_trade_id,
        last_trade_id,
        CASE
            WHEN ts_raw >= 100000000000000
                THEN TIMESTAMPTZ 'epoch'
                     + ts_raw * INTERVAL '1 microsecond'
            ELSE TIMESTAMPTZ 'epoch'
                 + ts_raw * INTERVAL '1 millisecond'
        END AS trade_time
    FROM dedu
),

previous AS (
    SELECT
        symbol,
        agg_trade_id,
        price,
        qty,
        first_trade_id,
        last_trade_id,
        trade_time,
        LAG(trade_time) OVER (
            PARTITION BY symbol
            ORDER BY trade_time, agg_trade_id
        ) AS previous_time
    FROM bounds
),

threshold AS (
    SELECT
        symbol,
        agg_trade_id,
        price,
        qty,
        first_trade_id,
        last_trade_id,
        trade_time,
        previous_time,
        CASE
            WHEN trade_time - previous_time > INTERVAL '2 seconds'
                THEN 1
            ELSE 0
        END AS thres
    FROM previous
),

summing AS (
    SELECT
        symbol,
        agg_trade_id,
        price,
        qty,
        first_trade_id,
        last_trade_id,
        trade_time,
        previous_time,
        thres,
        SUM(thres) OVER (
            PARTITION BY symbol
            ORDER BY trade_time, agg_trade_id
        ) AS burst_id
    FROM threshold
),

configure AS (
    SELECT
        symbol,
        MIN(trade_time) AS starttime,
        MAX(trade_time) AS endtime,
        MAX(trade_time) - MIN(trade_time) AS duration,

        SUM(last_trade_id - first_trade_id + 1) AS trade_count,

        SUM(qty) AS volume,

        SUM(price * qty) / SUM(qty) AS VWAP,

        burst_id,

        ARRAY_AGG(
            price
            ORDER BY trade_time, agg_trade_id
        ) AS price_array

    FROM summing
    GROUP BY symbol, burst_id
)

SELECT
    symbol,
    starttime,
    endtime,
    duration,
    trade_count,
    volume,
    VWAP,
    burst_id,

    price_array[array_length(price_array, 1)]
        - price_array[1] AS absolute_change

FROM configure;
-- 10. The 10 most intense bursts by trades per second (guard against zero durations), plus the distribution of burst durations per symbol.

-- Point-in-time

-- 11. For each trade, the most recent fully closed candle before it, and the trade's deviation from that close in basis points. The trap is look-ahead: a candle whose close time is after the trade isn't known yet.

-- 12. Attach the instrument_versions row valid at each trade's time. 
--Check: prove every trade matches exactly one version — including trades landing exactly on a boundary. 
--This is the SCD2 query pattern you'll build the other side of in week 11.
WITH dedu AS (
    SELECT
        symbol,
        agg_trade_id,
        price,
        qty,
        first_trade_id,
        last_trade_id,
        ts_raw,
        is_buyer_maker,
        is_best_match,
        source_file,
        batch_id,
        loaded_at
    FROM (
        SELECT *,
               ROW_NUMBER() OVER (
                   PARTITION BY symbol, agg_trade_id
                   ORDER BY loaded_at DESC
               ) AS rnk
        FROM raw.agg_trades
    ) t
    WHERE rnk = 1
),

converting AS (
    SELECT
        *,
        CASE
            WHEN ts_raw >= 100000000000000
                THEN TIMESTAMPTZ 'epoch'
                     + ts_raw * INTERVAL '1 microsecond'
            ELSE TIMESTAMPTZ 'epoch'
                 + ts_raw * INTERVAL '1 millisecond'
        END AS trade_time
    FROM dedu
)

SELECT *
FROM converting c
LEFT JOIN ref.instrument_versions r
    ON c.symbol = r.symbol
   AND c.trade_time >= r.valid_from
   AND c.trade_time < r.valid_to;
-- 13. (Stretch) Triangular dislocation: for each ETHBTC trade, take the as-of BTCUSDT and ETHUSDT prices, compute the implied price, and find the minutes of largest dislocation.

-- Reconciliation and rolling windows
-- 14. Per symbol-minute, reconcile trades against candles: summed quantity vs volume, summed price*qty vs quote volume, and trade count vs trade_count. Hint: count(*) is the wrong measure 
--— look at first_trade_id and last_trade_id. Then run it on the un-deduplicated trades and watch it break; that's the whole argument for Q2 in one result set.
WITH converting AS (
    SELECT
        symbol,
        last_trade_id,
        first_trade_id,
        price,
        qty,
        CASE
            WHEN ts_raw >= 100000000000000
                THEN TIMESTAMPTZ 'epoch'
                     + ts_raw * INTERVAL '1 microsecond'
            ELSE TIMESTAMPTZ 'epoch'
                 + ts_raw * INTERVAL '1 millisecond'
        END AS trade_time
    FROM raw.agg_trades
),

agg AS (
    SELECT
        symbol,
        MIN(trade_time) AS opentime,
        MAX(trade_time) AS closetime,

        SUM(last_trade_id - first_trade_id + 1) AS trade_count,

        SUM(price * qty) AS summedtotal,
        SUM(qty) AS summedqty,

        DATE_TRUNC('minute', trade_time) AS mincandle

    FROM converting
    GROUP BY
        symbol,
        DATE_TRUNC('minute', trade_time)
),

bounds AS (
    SELECT
        *,
        CASE
            WHEN open_time_raw >= 100000000000000
                THEN TIMESTAMPTZ 'epoch'
                     + open_time_raw * INTERVAL '1 microsecond'
            ELSE TIMESTAMPTZ 'epoch'
                 + open_time_raw * INTERVAL '1 millisecond'
        END AS open_time
    FROM raw.klines_1m
)

SELECT
    a.opentime,
    a.closetime,
    a.trade_count,
    a.summedtotal,
    a.summedqty,
    a.mincandle,

    b.volume,
    b.quote_volume,
    b.trade_count

FROM agg a
LEFT JOIN bounds b
    ON a.symbol = b.symbol
   AND a.mincandle = b.open_time;







WITH dedu AS (
  SELECT * FROM (
    SELECT *, row_number() OVER (PARTITION BY symbol, agg_trade_id
                                 ORDER BY loaded_at DESC, batch_id DESC) AS rnk
    FROM raw.agg_trades) t
  WHERE rnk = 1),
converting AS (
  SELECT symbol, first_trade_id, last_trade_id, price, qty,
         CASE WHEN ts_raw >= 100000000000000
              THEN TIMESTAMPTZ 'epoch' + ts_raw * INTERVAL '1 microsecond'
              ELSE TIMESTAMPTZ 'epoch' + ts_raw * INTERVAL '1 millisecond' END AS trade_time
  FROM dedu),
agg AS (
  SELECT symbol,
         date_trunc('minute', trade_time) AS minute,
         min(trade_time) AS first_trade_at,
         max(trade_time) AS last_trade_at,
         sum(last_trade_id - first_trade_id + 1) AS trades_calc,
         sum(qty)                                AS qty_calc,
         sum(price * qty)                        AS quote_calc
  FROM converting GROUP BY 1, 2),
kl AS (
  SELECT symbol, volume, quote_volume, trade_count,
         CASE WHEN open_time_raw >= 100000000000000
              THEN TIMESTAMPTZ 'epoch' + open_time_raw * INTERVAL '1 microsecond'
              ELSE TIMESTAMPTZ 'epoch' + open_time_raw * INTERVAL '1 millisecond' END AS open_time
  FROM raw.klines_1m),
recon AS (
  SELECT coalesce(a.symbol, k.symbol)       AS symbol,
         coalesce(a.minute, k.open_time)    AS minute,
         a.trades_calc, k.trade_count AS trades_kline,
         a.qty_calc,    k.volume       AS qty_kline,
         a.quote_calc,  k.quote_volume AS quote_kline,
         a.trades_calc - k.trade_count  AS count_diff,
         a.qty_calc    - k.volume       AS qty_diff,
         a.quote_calc  - k.quote_volume AS quote_diff
  FROM agg a FULL OUTER JOIN kl k
    ON a.symbol = k.symbol AND a.minute = k.open_time)
SELECT symbol,
       CASE WHEN trades_kline IS NULL          THEN 'TRADES_NO_CANDLE'
            WHEN trades_calc  IS NULL          THEN 'CANDLE_NO_TRADES'
            WHEN count_diff <> 0               THEN 'COUNT_MISMATCH'
            WHEN abs(qty_diff)   > 1e-8        THEN 'QTY_MISMATCH'
            WHEN abs(quote_diff) > 1e-8        THEN 'QUOTE_MISMATCH'
            ELSE 'OK' END AS verdict,
       count(*)           AS minutes,
       max(abs(count_diff)) AS worst_count_diff,
       max(abs(qty_diff))   AS worst_qty_diff,
       max(abs(quote_diff)) AS worst_quote_diff
FROM recon
GROUP BY 1, 2
ORDER BY 1, 3 DESC;
-- 15. (Stretch) 15-minute rolling VWAP and volatility. Then compare ROWS BETWEEN 14 PRECEDING against RANGE BETWEEN INTERVAL '14 minutes' PRECEDING across one of your Q5 outages, and explain why they differ. pgexercises never touches frame clauses; this is where you learn them.




--For each symbol, find the top 3 one-minute candles by trading volume. Return symbol, candle minute, volume, rank, 
--and only keep the top 3 per symbol. If two candles have the same volume, break ties by earlier candle time.

with choose as (select
symbol,CASE WHEN open_time_raw >= 100000000000000
            THEN TIMESTAMPTZ 'epoch' + open_time_raw * INTERVAL '1 microsecond'
            ELSE TIMESTAMPTZ 'epoch' + open_time_raw * INTERVAL '1 millisecond'
        END AS open_time,volume
from raw.klines_1m),

rankk as ( select symbol,open_time,volume,rank()over(partition by symbol order by volume desc,open_time asc)as rnk from choose)

select symbol,open_time,volume,rnk
from rankk
where rnk<=3

select * from raw.klines_1m



--For the recursive CTE:
--Starting from 2025-01-01 00:00:00+00, generate the next 10 consecutive 1-minute timestamps using a recursive CTE. Return sequence number and generated timestamp.

with recursive timelog as (
	select '2025-01-01 00:00:00+00' :: timestamp as generated_timestamp,
	1 as n

	Union All

	select generated_timestamp + interval '1 minute',
	n+1
	from timelog 
	where n<10
)

select * from timelog












