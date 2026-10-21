-- sql/ddl/01_bronze_meta.sql
-- A1 ingestion tables: raw landing (bronze) and the watermark (meta).
-- Idempotent: safe to run again. Run from the repo root:
--   psql_dw < sql/ddl/01_bronze_meta.sql

CREATE SCHEMA IF NOT EXISTS bronze;
CREATE SCHEMA IF NOT EXISTS meta;

-- One row per API response, exactly as received. Never updated, never deleted by the pipeline.
-- A page may appear more than once (a re-run after a crash re-fetches it): that is expected.
CREATE TABLE IF NOT EXISTS bronze.api_pages (
    id          BIGINT       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    endpoint    TEXT         NOT NULL,          -- e.g. /api/v3/aggTrades
    params      JSONB        NOT NULL,          -- the exact question asked
    fetched_at  TIMESTAMPTZ  NOT NULL,          -- when it was asked
    run_id      TEXT         NOT NULL,          -- same id as the run's log lines
    body        JSONB        NOT NULL           -- the answer, untouched
);

-- T030 parses "this run's pages": find them without scanning the whole table.
CREATE INDEX IF NOT EXISTS api_pages_run_id_idx ON bronze.api_pages (run_id);

-- The watermark: last aggregate trade id safely loaded into raw, per source and symbol.
-- Read by extract (T029); advanced only by load, in the same transaction as the data (T030).
CREATE TABLE IF NOT EXISTS meta.ingest_state (
    source             TEXT         NOT NULL,   -- e.g. binance_rest
    symbol             TEXT         NOT NULL,
    last_agg_trade_id  BIGINT       NOT NULL,
    updated_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (source, symbol)
);