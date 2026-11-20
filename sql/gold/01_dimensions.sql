-- sql/gold/01_dimensions.sql
-- T052 gold layer, file 2 of 4: the dimensions (docs/model.md section 5 and the ERD).
-- Idempotent. Order matters: venue and asset before instrument, which references both.
-- Every constraint below enforces an invariant from docs/model.md section 7.

-- Calendar, one row per UTC date. No unknown member: every fact has a timestamp (model.md D8).
CREATE TABLE IF NOT EXISTS gold.dim_date (
    -- Smart key YYYYMMDD, derived by the database so it can never disagree with full_date.
    date_key      int GENERATED ALWAYS AS (
                      (extract(year from full_date) * 10000
                     + extract(month from full_date) * 100
                     + extract(day from full_date))::int) STORED PRIMARY KEY,
    full_date     date     NOT NULL UNIQUE,
    year          smallint NOT NULL,
    quarter       smallint NOT NULL,
    month         smallint NOT NULL,
    month_name    text     NOT NULL,
    day_of_month  smallint NOT NULL,
    day_of_week   smallint NOT NULL,             -- ISO: 1 = Monday ... 7 = Sunday
    day_name      text     NOT NULL,
    is_weekend    boolean  NOT NULL
);

CREATE TABLE IF NOT EXISTS gold.dim_venue (
    venue_key   int  GENERATED ALWAYS AS IDENTITY PRIMARY KEY,   -- -1 = Unknown
    venue_code  text NOT NULL UNIQUE,                            -- natural key: binance, kraken
    venue_name  text NOT NULL
);

CREATE TABLE IF NOT EXISTS gold.dim_asset (
    asset_key   int  GENERATED ALWAYS AS IDENTITY PRIMARY KEY,   -- -1 = Unknown
    asset_code  text NOT NULL UNIQUE                             -- natural key: BTC, USDT
);

-- SCD2: one row per VERSION of an instrument. Natural key = (venue_key, symbol).
CREATE TABLE IF NOT EXISTS gold.dim_instrument (
    instrument_key    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,   -- -1 = Unknown
    venue_key         int    NOT NULL REFERENCES gold.dim_venue (venue_key),
    symbol            text   NOT NULL,
    -- Type 0: never change. dim_asset is an outrigger playing two roles (base, quote).
    base_asset_key    int    NOT NULL REFERENCES gold.dim_asset (asset_key),
    quote_asset_key   int    NOT NULL REFERENCES gold.dim_asset (asset_key),
    -- Type 2: a change creates a new version. Nullable only because the Unknown row has no values.
    tick_size         numeric,
    lot_step          numeric,
    min_notional      numeric,
    status            text   NOT NULL,                      -- Unknown row: 'Unknown'
    -- Half-open [valid_from, valid_to). valid_from = snapshot OBSERVATION time (UTC).
    valid_from        timestamptz NOT NULL,
    valid_to          timestamptz NOT NULL DEFAULT 'infinity',
    is_current        boolean     NOT NULL,
    is_inferred       boolean     NOT NULL DEFAULT false,   -- D3: history assumed before first snapshot
    attr_hash         text        NOT NULL,                 -- md5(row(tracked attributes)::text), T055
    -- Lineage to bronze.api_pages.id. Deliberately NOT a foreign key: gold must not block
    -- bronze maintenance, and gold-reset must not depend on bronze. NULL only for Unknown.
    snapshot_page_id  bigint,

    CONSTRAINT dim_instrument_interval_not_empty   CHECK (valid_from < valid_to),
    -- Together with the exclusion constraint, this allows at most ONE current row per key.
    CONSTRAINT dim_instrument_current_means_open   CHECK (is_current = (valid_to = 'infinity')),
    CONSTRAINT dim_instrument_version_start_unique UNIQUE (venue_key, symbol, valid_from),
    CONSTRAINT dim_instrument_no_overlap EXCLUDE USING gist (
        venue_key WITH =,
        symbol    WITH =,
        tstzrange(valid_from, valid_to, '[)') WITH &&
    )
);