"""T055: SCD2 load of gold.dim_instrument from exchangeInfo snapshots in bronze.

One snapshot = one transaction:
  0. lock + order check   refuse a snapshot older than the last one applied (forward-only)
  1. stage                snapshot JSON -> one row per symbol, tracked attributes, attr_hash
  2. assets               add base/quote asset codes not yet in gold.dim_asset
  3. Type 0 check         base/quote asset of an existing instrument must never change
  4. close                current rows whose hash differs: valid_to = observation time
  5. open                 new current row for changed keys and brand-new keys
  6. watermark            meta.snapshot_state = this snapshot

Rules carried from earlier tasks:
  - valid_from = snapshot OBSERVATION time (bronze fetched_at), not the exchange's real change time.
  - A key's first version starts at -infinity with is_inferred = true (model.md D3).
  - No ON CONFLICT on dim_instrument: it would silently swallow overlap errors (T052).
  - Symbols missing from a snapshot are left untouched (delisting is STRETCH).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import psycopg

from warehouse.errors import PermanentError, TransientError
from warehouse.extract import EXCHANGE_INFO_PATH

logger = logging.getLogger(__name__)

SOURCE = "binance_exchangeinfo"
VENUE_CODE = "binance"


@dataclass(frozen=True)
class SnapshotResult:
    page_id: int
    fetched_at: datetime
    symbols: int
    new: int
    changed: int
    unchanged: int
    skipped: bool = False  # True = this exact snapshot was already applied (expected replay)


# --- step 0 ------------------------------------------------------------------------------
# One loader at a time per source: a transaction-scoped advisory lock, released at COMMIT/ROLLBACK.
LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext('scd2:' || %(source)s))"

STATE_SQL = """
SELECT last_page_id, last_fetched_at FROM meta.snapshot_state WHERE source = %(source)s
"""

PAGE_SQL = """
SELECT id, fetched_at FROM bronze.api_pages WHERE id = %(page_id)s AND endpoint = %(endpoint)s
"""

VENUE_SQL = "SELECT venue_key FROM gold.dim_venue WHERE venue_code = %(venue_code)s"

# --- step 1 ------------------------------------------------------------------------------
# trim_scale(): Binance sends "0.01000000". 0.01000000 = 0.01 as numbers, but row(...)::text
# keeps the trailing zeros, so the md5 would differ if the format ever changed (verified).
# row(...)::text, not a || b: one NULL in a || b makes the whole hash NULL (plan T055).
# min_notional: the filter is NOTIONAL today, MIN_NOTIONAL on older snapshots. Check yours.
STAGE_SQL = """
CREATE TEMP TABLE stg_instrument ON COMMIT DROP AS
SELECT x.*,
       md5(row(x.tick_size, x.lot_step, x.min_notional, x.status)::text) AS attr_hash
FROM (
    SELECT s->>'symbol'     AS symbol,
           s->>'baseAsset'  AS base_asset,
           s->>'quoteAsset' AS quote_asset,
           s->>'status'     AS status,
           trim_scale((SELECT (f->>'tickSize')::numeric FROM jsonb_array_elements(s->'filters') f
                       WHERE f->>'filterType' = 'PRICE_FILTER')) AS tick_size,
           trim_scale((SELECT (f->>'stepSize')::numeric FROM jsonb_array_elements(s->'filters') f
                       WHERE f->>'filterType' = 'LOT_SIZE')) AS lot_step,
           trim_scale((SELECT (f->>'minNotional')::numeric FROM jsonb_array_elements(s->'filters') f
                       WHERE f->>'filterType' IN ('NOTIONAL', 'MIN_NOTIONAL')
                       ORDER BY f->>'filterType' = 'NOTIONAL' DESC LIMIT 1)) AS min_notional
    FROM bronze.api_pages p, jsonb_array_elements(p.body->'symbols') s
    WHERE p.id = %(page_id)s
) x
"""
# A symbol twice in one snapshot is a broken snapshot: fail loudly instead of picking one.
STAGE_KEY_SQL = "ALTER TABLE stg_instrument ADD PRIMARY KEY (symbol)"

# --- step 2 ------------------------------------------------------------------------------
# Plain reference rows keyed by their natural key: ON CONFLICT is fine HERE (not SCD2).
ASSETS_SQL = """
INSERT INTO gold.dim_asset (asset_code)
SELECT DISTINCT a FROM stg_instrument, LATERAL (VALUES (base_asset), (quote_asset)) v(a)
ON CONFLICT (asset_code) DO NOTHING
"""

# --- step 3 ------------------------------------------------------------------------------
TYPE0_SQL = """
SELECT s.symbol, b.asset_code || '/' || q.asset_code AS stored, s.base_asset || '/' || s.quote_asset AS seen
FROM stg_instrument s
JOIN gold.dim_instrument d ON d.venue_key = %(venue_key)s AND d.symbol = s.symbol AND d.is_current
JOIN gold.dim_asset b ON b.asset_key = d.base_asset_key
JOIN gold.dim_asset q ON q.asset_key = d.quote_asset_key
WHERE (b.asset_code, q.asset_code) IS DISTINCT FROM (s.base_asset, s.quote_asset)
"""

# --- step 4: close -----------------------------------------------------------------------
CLOSE_SQL = """
UPDATE gold.dim_instrument d
SET    valid_to = %(observed_at)s, is_current = false
FROM   stg_instrument s
WHERE  d.venue_key = %(venue_key)s
  AND  d.symbol    = s.symbol
  AND  d.is_current
  AND  d.attr_hash <> s.attr_hash
RETURNING d.symbol
"""

# --- step 5: open ------------------------------------------------------------------------
# Runs AFTER close, so "no current row" = brand-new key OR a key just closed.
# A key with no history at all gets its first version from -infinity (D3), flagged inferred.
OPEN_SQL = """
INSERT INTO gold.dim_instrument
       (venue_key, symbol, base_asset_key, quote_asset_key, tick_size, lot_step, min_notional, status,
        valid_from, is_current, is_inferred, attr_hash, snapshot_page_id)
SELECT %(venue_key)s, s.symbol, b.asset_key, q.asset_key, s.tick_size, s.lot_step, s.min_notional, s.status,
       CASE WHEN h.has_history THEN %(observed_at)s ELSE '-infinity'::timestamptz END,
       true,
       NOT h.has_history,
       s.attr_hash, %(page_id)s
FROM stg_instrument s
JOIN gold.dim_asset b ON b.asset_code = s.base_asset
JOIN gold.dim_asset q ON q.asset_code = s.quote_asset
CROSS JOIN LATERAL (
    SELECT EXISTS (SELECT 1 FROM gold.dim_instrument x
                   WHERE x.venue_key = %(venue_key)s AND x.symbol = s.symbol) AS has_history
) h
WHERE NOT EXISTS (SELECT 1 FROM gold.dim_instrument c
                  WHERE c.venue_key = %(venue_key)s AND c.symbol = s.symbol AND c.is_current)
RETURNING symbol, is_inferred
"""

# --- step 6: watermark -------------------------------------------------------------------
STATE_UPSERT_SQL = """
INSERT INTO meta.snapshot_state (source, last_page_id, last_fetched_at, updated_at)
VALUES (%(source)s, %(page_id)s, %(observed_at)s, now())
ON CONFLICT (source) DO UPDATE
SET last_page_id = EXCLUDED.last_page_id, last_fetched_at = EXCLUDED.last_fetched_at, updated_at = now()
"""

PENDING_SQL = """
SELECT id FROM bronze.api_pages
WHERE endpoint = %(endpoint)s
  AND (fetched_at, id) > (%(last_fetched_at)s, %(last_page_id)s)
ORDER BY fetched_at, id
"""
# Landed after the last applied page (higher id) but observed before it: out of order.
LATE_SQL = """
SELECT count(*) FROM bronze.api_pages
WHERE endpoint = %(endpoint)s AND id > %(last_page_id)s AND fetched_at < %(last_fetched_at)s
"""


def apply_snapshot(conn: psycopg.Connection, page_id: int) -> SnapshotResult:
    """Apply ONE exchangeInfo snapshot to gold.dim_instrument, in one transaction."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(LOCK_SQL, {"source": SOURCE})

        cur.execute(PAGE_SQL, {"page_id": page_id, "endpoint": EXCHANGE_INFO_PATH})
        page = cur.fetchone()
        if page is None:
            raise PermanentError(f"bronze page {page_id} is not an {EXCHANGE_INFO_PATH} snapshot")
        _, observed_at = page

        cur.execute(STATE_SQL, {"source": SOURCE})
        state = cur.fetchone()
        if state is not None:
            last_page_id, last_fetched_at = state
            if page_id == last_page_id:  # expected repetition: a no-op, not an error
                return SnapshotResult(page_id, observed_at, 0, 0, 0, 0, skipped=True)
            if (observed_at, page_id) < (last_fetched_at, last_page_id):  # a contradiction
                raise PermanentError(
                    f"snapshot page {page_id} (observed {observed_at}) is not newer than the last one "
                    f"applied (page {last_page_id}, {last_fetched_at}); applying it would reopen old "
                    "attributes. To insert history, rebuild: make gold-reset CONFIRM=yes, then replay."
                )

        cur.execute(VENUE_SQL, {"venue_code": VENUE_CODE})
        venue = cur.fetchone()
        if venue is None:
            raise PermanentError(f"venue {VENUE_CODE!r} not in gold.dim_venue - run: make gold")
        params = {"page_id": page_id, "observed_at": observed_at, "venue_key": venue[0], "source": SOURCE}

        cur.execute(STAGE_SQL, params)
        cur.execute(STAGE_KEY_SQL)
        cur.execute("SELECT count(*) FROM stg_instrument")
        symbols = cur.fetchone()[0]
        if symbols == 0:
            raise PermanentError(
                f"snapshot page {page_id} has no symbols - refusing to advance the watermark"
            )

        cur.execute(ASSETS_SQL)

        cur.execute(TYPE0_SQL, params)
        broken = cur.fetchall()
        if broken:
            shown = ", ".join(f"{s} {old} -> {new}" for s, old, new in broken[:5])
            raise PermanentError(f"Type 0 attribute changed for {len(broken)} symbol(s): {shown}")

        cur.execute(CLOSE_SQL, params)
        changed = len(cur.fetchall())
        cur.execute(OPEN_SQL, params)
        opened = cur.fetchall()
        new = sum(1 for _, inferred in opened if inferred)
        if len(opened) - new != changed:  # every closed key must have been reopened
            raise PermanentError(f"closed {changed} versions but opened {len(opened) - new}")

        cur.execute(STATE_UPSERT_SQL, params)

    return SnapshotResult(page_id, observed_at, symbols, new, changed, symbols - new - changed)


ALL_SQL = "SELECT id FROM bronze.api_pages WHERE endpoint = %(endpoint)s ORDER BY fetched_at, id"


def _pending_pages(conn: psycopg.Connection) -> list[int]:
    """Snapshots newer than the watermark, oldest first. Warns about out-of-order arrivals."""
    with conn.cursor() as cur:
        cur.execute(STATE_SQL, {"source": SOURCE})
        state = cur.fetchone()
        if state is None:  # nothing applied yet: every snapshot, in observation order
            cur.execute(ALL_SQL, {"endpoint": EXCHANGE_INFO_PATH})
            return [r[0] for r in cur.fetchall()]
        keys = {"endpoint": EXCHANGE_INFO_PATH, "last_page_id": state[0], "last_fetched_at": state[1]}
        cur.execute(LATE_SQL, keys)
        late = cur.fetchone()[0]
        if late:
            logger.warning(
                "%s snapshot(s) landed after page %s but were observed before it: NOT applied. "
                "To include them: make gold-reset CONFIRM=yes, then run this again.",
                late,
                state[0],
            )
        cur.execute(PENDING_SQL, keys)
        return [r[0] for r in cur.fetchall()]


def run_scd2(dsn: str, page_id: int | None = None) -> list[SnapshotResult]:
    """Apply one snapshot (page_id) or every pending one in observation order, one transaction each."""
    results: list[SnapshotResult] = []
    try:
        # autocommit: every conn.transaction() below is a real BEGIN/COMMIT, never a savepoint
        with psycopg.connect(dsn, autocommit=True) as conn:
            pages = [page_id] if page_id is not None else _pending_pages(conn)
            if not pages:
                logger.info("no pending %s snapshots", SOURCE)
            for pid in pages:
                r = apply_snapshot(conn, pid)
                results.append(r)
                if r.skipped:
                    logger.info("snapshot page %s already applied - nothing to do", pid)
                else:
                    logger.info(
                        "snapshot page %s (observed %s): %s symbols, %s new, %s changed, %s unchanged",
                        pid,
                        r.fetched_at,
                        r.symbols,
                        r.new,
                        r.changed,
                        r.unchanged,
                    )
    except psycopg.Error as exc:
        first = str(exc).splitlines()[0]
        if isinstance(exc, psycopg.errors.UndefinedTable):
            raise PermanentError(f"{first} - run: make up && make gold") from exc
        if isinstance(exc, psycopg.OperationalError):
            raise TransientError(f"database unavailable: {first}") from exc
        # constraint violations land here, named (e.g. dim_instrument_no_overlap): a loader bug
        raise PermanentError(f"database error: {first}") from exc
    return results
