## Query 2 — Deduplicate and keep the latest

### Problem
Query scanned a large portion of `raw.agg_trades`.

### Before
- Plan: Seq Scan/Parallel Seq Scan
- Execution time: 14.15s
- Actual rows:7.8M
- Major expensive nodes: ordering on the symbol and agg_trade_id and then sort node across 9.1M rows casue large spill to disk
- Disk spill:325MB
- Rows removed by filter: filter rank<=1, 1.3M

### Change
Added index:
Create Index sorted_agg on raw.agg_trades (symbol,agg_trade_id,loaded_at desc)

index may allow PostgreSQL to obtain those 9M rows in the required order without performing the massive external sort.


### After
------
Execution time:16.02
Plan change: Index Scan using sorted_agg
Disk spill:-
Rows processed:9,113,818
Improvement: Huge explicit sort is discarded, improve in dimension, No need parallel worker

Why it improved:
The index therefore provides little selectivity benefit and
requires scanning millions of index entries plus accessing
the underlying heap rows.The time increased but the index successfully optimized ordering



## Query 3 — Use the distinct on to deduplicate

### Problem
Parallel Seq Scan
→ Sort
→ Gather Merge
→ Unique  the Sort uses external merge, which means the sort spilled to disk.

### Before
- Plan: Seq Scan
- Execution time: 28.78s
- Actual rows:7.8M
- Major expensive nodes: The sorting node on symbol,agg_trade_id and loaded_at is out of working memory and write sorted chunk into disk
- Disk spill:320MB
- Rows removed by filter: Unique remove 1.3M

### Change
Added index: Create Index sorted_agg on raw.agg_trades (symbol,agg_trade_id,loaded_at desc)


### After
------
Execution time:10.87s
Plan change: Index Scan using sorted_agg
Disk spill:--
Rows processed:9,113,818 rows
Improvement:The execution time improve largely due to no need to calculate the row number again

Why it improved:
The distinct on exploit the index order (symbol, agg_trade_id, loaded_at DESC) which perfectly match with what the distinct on



## Query 4 — Find the missing minute with generate_series

### Problem
Seq Scan
→ HashAggregate
→ ProjectSet
→ Sort
                         \
                          Merge Left Join
                         /
Seq Scan
→ Sort
→ Filter

No big problem

### Before
- Plan: Seq Scan
- Execution time: 0.54s
- Actual rows:30
- Major expensive nodes: no expensive run since the table is relatively small and sort with quicksort which indicate good fit in memory
- Disk spill:--
- Rows removed by filter: Filtre remove 17250

### Change
Added index: No change!!
Analyzed the plan and decided not to add an index because the table is small, all rows are required, the sorts fit comfortably in memory, and symbol has low selectivity.


### After
------
Execution time:
Plan change:
Disk spill:
Rows processed:
Improvement:

Why it improved:
...

```sql
CREATE INDEX ...
```


## Query 8 — Sessionisation,Burst_Id

### Problem
The first large sort does not produce the ordering required by the later sessionisation window, so PostgreSQL has to reorder millions of rows again.
Seq/Parallel Seq Scan ~9.1M.The planner badly underestimates how many rows survive the dedup/window stage.
→ large Sort for dedup
→ WindowAgg ROW_NUMBER
→ keep ~7.9M
→ Incremental Sort for trade-time order
→ WindowAgg LAG
→ WindowAgg running SUM

### Before
- Plan: Seq Scan
- Execution time: 17.9s
- Actual rows:7.9M
- Major expensive nodes: Still the sorting order for the large table cost a lot. 
- Disk spill:271MB
- Rows removed by filter: Filtre remove 1.3M

### Change
Added index: Create Index sorted_agg on raw.agg_trades (symbol,agg_trade_id,loaded_at desc)


### After
------
Execution time:16.97
Plan change:
Index Scan using sorted_agg
→ WindowAgg ROW_NUMBER
→ ~7.9M rows
→ Incremental Sort for trade_time
→ WindowAgg LAG
→ WindowAgg SUM
Disk spill:Dedup sort spill removed,
but Incremental Sort for sessionization still spills ~271MB
Rows processed:~9.1M scanned
→ ~7.9M deduplicated rows
→ ~7.9M rows still processed by sessionization windows
Improvement: ~0.9s

Why it improved:
The later sessionization stage requires a different ordering
(symbol, trade_time, agg_trade_id),
so PostgreSQL still performs a large incremental sort over ~7.9M rows.
```sql
CREATE INDEX ...
```

## Query 12 — Attach to instructment to validate through the equality (SCD2)

### Problem
Small Table, quicksort
Seq Scan
→ Sort

Parallel Seq Scan
→ large Sort
→ Gather Merge
→ WindowAgg
→ dedup
→ ~7.9M rows

### Before
- Plan: Seq Scan and then Parallel Scan
- Execution time: 15.16s
- Actual rows:7.9M
- Major expensive nodes: Still the sorting order for the large table cost a lot.The optimizer expect the row to be reduced a lot after the join
- Disk spill:331MB
- Rows removed by filter: FiltER remove 4.87M

The join finds candidate rows with the same symbol, but millions of candidate version pairs fail the validity-range condition.

### Change
Added index: Create Index sorted_agg on raw.agg_trades (symbol,agg_trade_id,loaded_at desc)


### After
------
Execution time: 9.9s
Plan change:
9.1M trades through ordered index
→ dedup
→ 7.9M
→ hash join tiny 5-row SCD2 table
Disk spill:--
Rows processed: ~9.1M processed
→ ~7.9M deduplicated rows
Improvement:~6s

Why it improved:
The composite index removed the large sort required by the deduplication stage. Unlike the sessionisation query, Q12 does not require another large reordering of the deduplicated trades, so the benefit carries directly into the final join. PostgreSQL also switched to a Hash Left Join, building a tiny hash table from the 5-row instrument_versions table. The validity-range join filter still evaluates millions of candidate pairs, but this is no longer the dominant bottleneck.



## T20 — Unfamiliar Plan Diagnosis
EXPLAIN ANALYZE
SELECT
    a.symbol,
    a.agg_trade_id,
    a.price,
    a.qty,
    a.ts_raw
FROM raw.agg_trades a
JOIN raw.klines_1m k
    ON a.symbol = k.symbol
WHERE
    CASE
        WHEN a.ts_raw >= 100000000000000
            THEN TIMESTAMPTZ 'epoch'
                 + a.ts_raw * INTERVAL '1 microsecond'
        ELSE TIMESTAMPTZ 'epoch'
             + a.ts_raw * INTERVAL '1 millisecond'
    END
    >=
    CASE
        WHEN k.open_time_raw >= 100000000000000
            THEN TIMESTAMPTZ 'epoch'
                 + k.open_time_raw * INTERVAL '1 microsecond'
        ELSE TIMESTAMPTZ 'epoch'
             + k.open_time_raw * INTERVAL '1 millisecond'
    END
AND
    CASE
        WHEN a.ts_raw >= 100000000000000
            THEN TIMESTAMPTZ 'epoch'
                 + a.ts_raw * INTERVAL '1 microsecond'
        ELSE TIMESTAMPTZ 'epoch'
             + a.ts_raw * INTERVAL '1 millisecond'
    END
    <
    CASE
        WHEN k.close_time_raw >= 100000000000000
            THEN TIMESTAMPTZ 'epoch'
                 + k.close_time_raw * INTERVAL '1 microsecond'
        ELSE TIMESTAMPTZ 'epoch'
             + k.close_time_raw * INTERVAL '1 millisecond'
    END;



## Original plan
FIRST PASS
9.1M rows
→ calculate AVG(price)
→ ~$1.74 sec

SECOND PASS
9.1M rows
→ filter price > average
→ ~6.0M survive
→ calculate trade_hour
→ external sort of ~6.0M rows
→ aggregate

Important observations:
- The table is scanned twice:
  1. once to calculate the global AVG(price)
  2. once again to filter rows using price > AVG(price)
- Around 6M rows survive the filter, so the predicate is not very selective.
- The surviving rows are sorted by `(symbol, trade_hour)`.
- The sort uses `external merge`, so it spills to disk.
- The planner estimates around 744k groups, while the actual result is only around 96 groups.

## One-sentence diagnosis
The query is expensive because it scans the 9.1M-row table once to compute the global average and again to filter the trades, after which roughly 6M surviving rows must be externally sorted by (symbol, trade_hour), causing disk spill.

## Predicted fix
Initially, I predicted that precomputing the timestamp/hour value would reduce repeated per-row timestamp conversion and make the grouping expression easier for PostgreSQL to optimize.

The expected improvement was:

raw `ts_raw`
→ normalize timestamp once
→ query directly on `trade_ts_utc`
→ reduce repeated CASE/timestamp conversion work

## Change
Created a staging table with a normalized UTC timestamp:

```sql
CREATE SCHEMA IF NOT EXISTS stg;

CREATE TABLE stg.agg_trades AS
SELECT
    symbol,
    agg_trade_id,
    price,
    qty,
    CASE
        WHEN ts_raw >= 100000000000000
            THEN TIMESTAMP 'epoch'
                 + ts_raw * INTERVAL '1 microsecond'
        ELSE TIMESTAMP 'epoch'
             + ts_raw * INTERVAL '1 millisecond'
    END AS trade_ts_utc
FROM raw.agg_trades;

ANALYZE stg.agg_trades

```

## New plan
The overall expensive pattern remained largely unchanged:
9.1M rows
→ calculate AVG(price)
9.1M rows
→ filter price > average
→ ~6M rows survive
→ large external sort
→ GroupAggregate
The sort still spilled to disk, so the dominant bottleneck was not removed.
The planner also still had a very large group-cardinality misestimate:

## Result
Before:
Execution time: ~3.99s
~9.1M rows scanned for AVG(price)
~9.1M rows scanned again for filtering
~6M rows survive
Large external sort
Disk spill: roughly 70–90MB per worker
Actual groups: ~96

After:
Execution time: ~4.38s in the observed run
Large external sort still present
Disk spill still present
Similar number of rows processed
No meaningful end-to-end performance improvement

## Conclusion
The original diagnosis was mostly correct: the dominant cost is the large amount of data processed and especially the external sort of roughly 6M rows.
However, the predicted fix was not sufficient.
Precomputing the timestamp improves the data model and avoids repeated timestamp-cleaning logic, but timestamp conversion was not the main bottleneck. The query still needs to process millions of rows and externally sort them.




































