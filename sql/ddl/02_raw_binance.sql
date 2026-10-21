-- sql/ddl/02_raw_binance.sql
-- A1 raw layer: one row per Binance aggregate trade, typed and keyed.
-- Source grain and source meaning; only mechanical conversion (JSON -> columns,
-- text -> NUMERIC, ms -> us). Not the week-3 lab table raw.agg_trades.
-- Idempotent. Run after 01_bronze_meta.sql:   psql_dw < sql/ddl/02_raw_binance.sql

CREATE SCHEMA IF NOT EXISTS raw;

CREATE TABLE IF NOT EXISTS raw.binance_agg_trades (
    symbol          TEXT           NOT NULL,
    agg_trade_id    BIGINT         NOT NULL,
    price           NUMERIC(28,8)  NOT NULL,
    qty             NUMERIC(28,8)  NOT NULL,
    first_trade_id  BIGINT         NOT NULL,
    last_trade_id   BIGINT         NOT NULL,
    ts_us           BIGINT         NOT NULL,   -- always microseconds since epoch
    is_buyer_maker  BOOLEAN        NOT NULL,
    is_best_match   BOOLEAN        NOT NULL,
    source          TEXT           NOT NULL,   -- lineage: binance_rest | binance_bulk
    bronze_page_id  BIGINT,                    -- lineage: bronze.api_pages.id (REST only)
    loaded_at       TIMESTAMPTZ    NOT NULL DEFAULT now(),
    -- The natural key: a trade of a symbol exists once. Makes every load idempotent.
    PRIMARY KEY (symbol, agg_trade_id)
);