-- sql/gold/02_facts.sql
-- T052 gold layer, file 3 of 4: the facts. Idempotent. Needs 01_dimensions.sql.
-- Each primary key IS the table's grain statement (docs/model.md section 3).
-- Indexes: only those the constraints create. Add more when EXPLAIN shows a query needs one.

-- Grain: one row per Binance aggregate trade, as published.
CREATE TABLE IF NOT EXISTS gold.fact_trade (
    venue_key       int    NOT NULL REFERENCES gold.dim_venue (venue_key),
    symbol          text   NOT NULL,
    agg_trade_id    bigint NOT NULL,                       -- degenerate dimension
    instrument_key  bigint NOT NULL REFERENCES gold.dim_instrument (instrument_key),  -- version at trade time, -1 if none
    trade_time      timestamptz NOT NULL,
    -- UTC date of the trade, derived by the database (cannot disagree with trade_time).
    date_key        int GENERATED ALWAYS AS (
                        (extract(year  from trade_time AT TIME ZONE 'UTC') * 10000
                       + extract(month from trade_time AT TIME ZONE 'UTC') * 100
                       + extract(day   from trade_time AT TIME ZONE 'UTC'))::int) STORED
                        NOT NULL REFERENCES gold.dim_date (date_key),
    price           numeric(28,8) NOT NULL,                -- non-additive
    qty             numeric(28,8) NOT NULL,                -- additive within one instrument
    quote_qty       numeric GENERATED ALWAYS AS (price * qty) STORED,     -- exact, up to 16 dp
    first_trade_id  bigint NOT NULL,                       -- degenerate
    last_trade_id   bigint NOT NULL,                       -- degenerate
    fill_count      int GENERATED ALWAYS AS ((last_trade_id - first_trade_id + 1)::int) STORED,
    is_buyer_maker  boolean NOT NULL,
    is_best_match   boolean NOT NULL,
    source          text NOT NULL,                         -- which path loaded it: ms vs us precision
    load_run_id     text NOT NULL,

    CONSTRAINT fact_trade_pk               PRIMARY KEY (venue_key, symbol, agg_trade_id),
    CONSTRAINT fact_trade_fill_range_valid CHECK (last_trade_id >= first_trade_id),
    CONSTRAINT fact_trade_source_known     CHECK (source IN ('binance_rest', 'binance_bulk'))
);

-- Grain: one row per venue x instrument x UTC minute in which at least one aggregate trade
-- happened (SPARSE, model.md D5). Built from aggregate trades (D1).
CREATE TABLE IF NOT EXISTS gold.fact_market_minute (
    venue_key       int         NOT NULL REFERENCES gold.dim_venue (venue_key),
    symbol          text        NOT NULL,
    minute_utc      timestamptz NOT NULL,                  -- start of the minute [t, t + 1 min)
    instrument_key  bigint      NOT NULL REFERENCES gold.dim_instrument (instrument_key),  -- version at minute start (D6)
    date_key        int GENERATED ALWAYS AS (
                        (extract(year  from minute_utc AT TIME ZONE 'UTC') * 10000
                       + extract(month from minute_utc AT TIME ZONE 'UTC') * 100
                       + extract(day   from minute_utc AT TIME ZONE 'UTC'))::int) STORED
                        NOT NULL REFERENCES gold.dim_date (date_key),
    open            numeric(28,8) NOT NULL,                -- first trade, by (trade_time, agg_trade_id)
    high            numeric(28,8) NOT NULL,
    low             numeric(28,8) NOT NULL,
    close           numeric(28,8) NOT NULL,                -- last trade
    volume          numeric(28,8) NOT NULL,                -- sum of qty: same scale, stays exact
    quote_volume    numeric       NOT NULL,                -- sum of price * qty: unconstrained, exact
    agg_trade_rows  int           NOT NULL,                -- count of aggregate trades
    trade_count     int           NOT NULL,                -- sum of fills (what candles count)
    load_run_id     text          NOT NULL,

    CONSTRAINT fact_market_minute_pk           PRIMARY KEY (venue_key, symbol, minute_utc),
    CONSTRAINT fact_market_minute_whole_minute CHECK (minute_utc = date_trunc('minute', minute_utc, 'UTC')),
    CONSTRAINT fact_market_minute_sparse       CHECK (agg_trade_rows >= 1),          -- D5 as a rule
    CONSTRAINT fact_market_minute_fills        CHECK (trade_count >= agg_trade_rows),  -- each row bundles >= 1 fill
    CONSTRAINT fact_market_minute_ohlc         CHECK (low <= open AND open <= high AND low <= close AND close <= high)
);