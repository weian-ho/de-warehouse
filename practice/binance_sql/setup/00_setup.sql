-- sql/analytical/00_setup.sql
-- Week 3 analytical dataset: Binance spot public market data (data.binance.vision).
--
-- Raw tables deliberately have NO primary key: duplicates are part of the exercise.
-- Prices are NUMERIC, never float: money in floats is a bug you will meet again in week 13.
-- Timestamps are kept as raw integers here. Converting them correctly is exercise 0.

CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS ref;

-- ---------------------------------------------------------------- aggTrades
DROP TABLE IF EXISTS raw.agg_trades_stage;
CREATE TABLE raw.agg_trades_stage (          -- exact shape of the CSV file
    agg_trade_id    BIGINT,
    price           NUMERIC(28,8),
    qty             NUMERIC(28,8),
    first_trade_id  BIGINT,
    last_trade_id   BIGINT,
    ts_raw          BIGINT,
    is_buyer_maker  BOOLEAN,
    is_best_match   BOOLEAN
);

DROP TABLE IF EXISTS raw.agg_trades;
CREATE TABLE raw.agg_trades (
    symbol          TEXT          NOT NULL,
    agg_trade_id    BIGINT        NOT NULL,
    price           NUMERIC(28,8) NOT NULL,
    qty             NUMERIC(28,8) NOT NULL,
    first_trade_id  BIGINT,
    last_trade_id   BIGINT,
    ts_raw          BIGINT        NOT NULL,
    is_buyer_maker  BOOLEAN,
    is_best_match   BOOLEAN,
    source_file     TEXT          NOT NULL,
    batch_id        INT           NOT NULL,
    loaded_at       TIMESTAMPTZ   NOT NULL DEFAULT clock_timestamp()
);

-- ---------------------------------------------------------------- 1m klines
DROP TABLE IF EXISTS raw.klines_1m_stage;
CREATE TABLE raw.klines_1m_stage (           -- exact shape of the CSV file
    open_time_raw    BIGINT,
    open_px          NUMERIC(28,8),
    high_px          NUMERIC(28,8),
    low_px           NUMERIC(28,8),
    close_px         NUMERIC(28,8),
    volume           NUMERIC(28,8),
    close_time_raw   BIGINT,
    quote_volume     NUMERIC(28,8),
    trade_count      BIGINT,
    taker_buy_base   NUMERIC(28,8),
    taker_buy_quote  NUMERIC(28,8),
    ignore_col       TEXT
);

DROP TABLE IF EXISTS raw.klines_1m;
CREATE TABLE raw.klines_1m (
    symbol           TEXT          NOT NULL,
    open_time_raw    BIGINT        NOT NULL,
    open_px          NUMERIC(28,8) NOT NULL,
    high_px          NUMERIC(28,8) NOT NULL,
    low_px           NUMERIC(28,8) NOT NULL,
    close_px         NUMERIC(28,8) NOT NULL,
    volume           NUMERIC(28,8) NOT NULL,
    close_time_raw   BIGINT        NOT NULL,
    quote_volume     NUMERIC(28,8),
    trade_count      BIGINT,
    taker_buy_base   NUMERIC(28,8),
    taker_buy_quote  NUMERIC(28,8),
    ignore_col       TEXT,
    source_file      TEXT          NOT NULL,
    batch_id         INT           NOT NULL,
    loaded_at        TIMESTAMPTZ   NOT NULL DEFAULT clock_timestamp()
);

-- ---------------------------------------------------------------- reference
-- SYNTHETIC versioned instrument metadata, for point-in-time join practice only.
-- These values are invented. Real exchangeInfo history is not downloadable, which is
-- why the capstone starts snapshotting it daily from week 5.
-- Intervals are half-open: valid_from <= t < valid_to.
DROP TABLE IF EXISTS ref.instrument_versions;
CREATE TABLE ref.instrument_versions (
    symbol        TEXT          NOT NULL,
    tick_size     NUMERIC(28,8) NOT NULL,
    min_notional  NUMERIC(28,8) NOT NULL,
    valid_from    TIMESTAMPTZ   NOT NULL,
    valid_to      TIMESTAMPTZ   NOT NULL,
    note          TEXT          NOT NULL
);

INSERT INTO ref.instrument_versions VALUES
  ('BTCUSDT', 0.01,     5,      '2024-01-01 00:00+00', '2025-01-01 08:00+00', 'SYNTHETIC'),
  ('BTCUSDT', 0.10,     10,     '2025-01-01 08:00+00', 'infinity',            'SYNTHETIC'),
  ('ETHUSDT', 0.01,     5,      '2024-01-01 00:00+00', 'infinity',            'SYNTHETIC'),
  ('ETHBTC',  0.00001,  0.0001, '2024-01-01 00:00+00', '2024-12-31 12:00+00', 'SYNTHETIC'),
  ('ETHBTC',  0.000001, 0.0001, '2024-12-31 12:00+00', 'infinity',            'SYNTHETIC');
