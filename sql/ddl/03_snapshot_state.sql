-- sql/ddl/03_snapshot_state.sql
-- T055: the forward-only watermark for reference snapshots, the A1 ingest_state idea applied
-- to a dimension. One row per snapshot source. Moved ONLY by the SCD2 loader, in the same
-- transaction as the dimension changes it describes. Idempotent.

CREATE TABLE IF NOT EXISTS meta.snapshot_state (
    source           text        PRIMARY KEY,            -- e.g. 'binance_exchangeinfo'
    last_page_id     bigint      NOT NULL,               -- bronze.api_pages.id last applied
    last_fetched_at  timestamptz NOT NULL,               -- its fetched_at = observation time
    updated_at       timestamptz NOT NULL DEFAULT now()
);
