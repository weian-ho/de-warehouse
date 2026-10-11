## Project Domain
Cross-venue market surveillance and reconciliation




## Criteria
## 1: Incremental Ingestion and recovery

### Question
1. Does the Ingestion process of the sources force us to tell us the incremental state,continuation,pagination

2. What happen if request is failed

3. What happen if the rate limit is hitted.

### Binance domain mechanism

1. IDcursor: fromId to tell where should I continue
2. Operational Behaviour
    - 429: rate limtied 
    - 418: banned/stop
    - 503:transient failure
    - timeout

### Evidence 
- client.py
- extract.py

meta.ingest_state:source,symbol,last_agg_trade_id,updated_at

--------

## Criteria
## 2: Raw Replayability

### Question
1. Does we able to retain and replay the raw acquisition when the historical response has changed or API is unavailable?

2. How do I reproduce the what happened in the old run?

### Binance domain mechanism

1. REST pages → bronze.api_pages
fetch
 ↓
LAND RAW
 ↓
parse
 ↓
load

### Evidence 
- bronze.api_pages
id
endpoint
params
fetched_at
run_id
body

## Criteria
## 3: Idempotent & state-safe loading

### Question
1. Can I safely run the pipeline again?

2. Does the domain provide a stable natural key and allow us to demonstrate safe reruns, crash recovery and historical backfills?


### Binance domain mechanism

1. PRIMARY KEY (symbol, agg_trade_id)
2. ON CONFLICT (symbol, agg_trade_id)
DO NOTHING
3. Transactional watermark
(BEGIN

insert rows
update watermark

COMMIT)

(ROLLBACK

rows = unchanged
watermark = unchanged)

### Evidence 
run twice and get identical counts, and the crash test (zero rows committed, watermark unchanged)


## Criteria
## 4: Independent correctness verification

### Question
1. Can correctness be proven against another ingestion path/source of truth?

### Binance domain mechanism

1. Source A
   ↓
pipeline A

vs

Source B
   ↓
pipeline B

Do they agree?

```
REST API
vs
historical bulk files
```


### Evidence 
count(*)
sum(qty)
min(agg_trade_id)
max(agg_trade_id)


                    BINANCE DOMAIN
                           │
        ┌──────────────────┼───────────────────┐
        ▼                  ▼                   ▼
  acquisition          persistence          evidence
        │                  │                   │
    client.py          bronze/raw          validate.py
    extract.py         ingest_state        pytest
        │                  │              reconciliation
        └──────────────────┼───────────────────┘
                           ▼
                        load.py














