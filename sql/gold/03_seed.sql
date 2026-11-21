-- sql/gold/03_seed.sql
-- T052 gold layer, file 4 of 4: rows the model needs before any load. Idempotent.

-- Unknown members (key -1): a fact that arrives before its dimension row points here
-- instead of at NULL, so it never drops out of an inner join.
-- GENERATED ALWAYS rejects an explicit key unless OVERRIDING SYSTEM VALUE is written.
-- ON CONFLICT DO NOTHING is safe HERE ONLY. Never use it for dim_instrument in the SCD2 load:
-- it also silently swallows exclusion-constraint (overlap) violations.
INSERT INTO gold.dim_venue (venue_key, venue_code, venue_name)
    OVERRIDING SYSTEM VALUE VALUES (-1, 'unknown', 'Unknown')
    ON CONFLICT DO NOTHING;

INSERT INTO gold.dim_asset (asset_key, asset_code)
    OVERRIDING SYSTEM VALUE VALUES (-1, 'Unknown')
    ON CONFLICT DO NOTHING;

INSERT INTO gold.dim_instrument (instrument_key, venue_key, symbol, base_asset_key, quote_asset_key,
                                 status, valid_from, valid_to, is_current, is_inferred, attr_hash)
    OVERRIDING SYSTEM VALUE VALUES (-1, -1, 'Unknown', -1, -1,
                                    'Unknown', '-infinity', 'infinity', true, false, 'unknown')
    ON CONFLICT DO NOTHING;

-- Reference data: the venues the model is built for (Kraken is modelled now, loaded later).
-- On a re-run the conflicting rows still use up identity values: harmless gaps,
-- because a surrogate key carries no meaning.
INSERT INTO gold.dim_venue (venue_code, venue_name)
VALUES ('binance', 'Binance'), ('kraken', 'Kraken')
ON CONFLICT (venue_code) DO NOTHING;

-- Calendar: covers the backfill (2024-12-31) with room to spare. To extend, change the end
-- date and run `make gold` again: existing dates are skipped.
-- FM = no padding; non-TM names are English whatever the server locale.
INSERT INTO gold.dim_date (full_date, year, quarter, month, month_name,
                           day_of_month, day_of_week, day_name, is_weekend)
SELECT d,
       extract(year    from d)::smallint,
       extract(quarter from d)::smallint,
       extract(month   from d)::smallint,
       to_char(d, 'FMMonth'),
       extract(day     from d)::smallint,
       extract(isodow  from d)::smallint,
       to_char(d, 'FMDay'),
       extract(isodow  from d) IN (6, 7)
FROM generate_series(date '2024-01-01', date '2030-12-31', interval '1 day') AS g(ts),
     LATERAL (SELECT g.ts::date AS d) x
ON CONFLICT (full_date) DO NOTHING;