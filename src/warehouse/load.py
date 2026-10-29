"""Parse a run's bronze pages into raw.binance_agg_trades and move the watermark.

    bronze.api_pages (WHERE run_id = X) ──► parse ──► AggTrade ──► INSERT … ON CONFLICT DO NOTHING
                                                                          │   (same transaction)
                                                       meta.ingest_state ◄┘   watermark update

Reads only from bronze, never from the API, so any run can be re-loaded later
(`warehouse load --run-id X`). Loading the same pages twice inserts nothing new.
"""

import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import psycopg
from psycopg import sql

from warehouse.client import AGG_TRADES_PATH
from warehouse.errors import PermanentError, TransientError
from warehouse.extract import DDL_HINT, SOURCE
from warehouse.models import AggTrade
from warehouse.transform import normalise_ts_to_us

logger = logging.getLogger(__name__)

REQUIRED_KEYS = ("a", "p", "q", "f", "l", "T", "m", "M")

DEFAULT_TARGET = "raw.binance_agg_trades"

INSERT_SQL = sql.SQL("""
    INSERT INTO {table}
        (symbol, agg_trade_id, price, qty, first_trade_id, last_trade_id,
         ts_us, is_buyer_maker, is_best_match, source, bronze_page_id)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (symbol, agg_trade_id) DO NOTHING
""")

UPSERT_WATERMARK_SQL = """
    INSERT INTO meta.ingest_state (source, symbol, last_agg_trade_id, updated_at)
    VALUES (%s, %s, %s, now())
    ON CONFLICT (source, symbol) DO UPDATE
    SET last_agg_trade_id = GREATEST(meta.ingest_state.last_agg_trade_id, EXCLUDED.last_agg_trade_id),
        updated_at        = now()
"""


@dataclass(frozen=True)
class LoadResult:
    run_id: str
    symbol: str
    pages: int
    offered: int  # trades parsed from bronze
    inserted: int  # trades that were new to raw
    watermark_before: int | None
    watermark_after: int | None


# ---- parsing: bronze JSON -> AggTrade (pure, no database) ---------------------------------
def parse_trade(symbol: str, rec: dict[str, Any]) -> AggTrade:
    """One aggTrades JSON object -> AggTrade. Raises KeyError / ValueError / TypeError on bad data."""
    missing = [k for k in REQUIRED_KEYS if k not in rec]
    if missing:
        raise KeyError(f"missing keys {missing}")
    if not isinstance(rec["p"], str) or not isinstance(rec["q"], str):
        raise TypeError("price/qty must arrive as strings")  # a float here would already be inexact
    return AggTrade(
        symbol=symbol,
        agg_trade_id=int(rec["a"]),
        price=Decimal(rec["p"]),
        qty=Decimal(rec["q"]),
        first_trade_id=int(rec["f"]),
        last_trade_id=int(rec["l"]),
        ts_us=normalise_ts_to_us(int(rec["T"])),
        is_buyer_maker=bool(rec["m"]),
        is_best_match=bool(rec["M"]),
    )


def parse_page(symbol: str, body: Any, bronze_id: int) -> list[AggTrade]:
    """A whole bronze page -> list of AggTrade. Any bad record fails the page, naming where."""
    if not isinstance(body, list):
        raise PermanentError(f"bronze page {bronze_id}: expected a JSON list, got {type(body).__name__}")
    trades = []
    for i, rec in enumerate(body):
        try:
            trades.append(parse_trade(symbol, rec))
        except (KeyError, ValueError, TypeError, InvalidOperation) as exc:
            raise PermanentError(f"bronze page {bronze_id}, record {i}: {exc}") from exc
    return trades


# ---- loading: one transaction for data + watermark ----------------------------------------
def _read_watermark_for_update(conn: psycopg.Connection, symbol: str) -> int | None:
    """Read the watermark and lock its row until COMMIT, so two loads cannot race."""
    row = conn.execute(
        "SELECT last_agg_trade_id FROM meta.ingest_state WHERE source = %s AND symbol = %s FOR UPDATE",
        (SOURCE, symbol),
    ).fetchone()
    return None if row is None else int(row[0])


def load_run(
    dsn: str,
    run_id: str,
    symbol: str,
    target: str = DEFAULT_TARGET,
    advance_watermark: bool = True,
) -> LoadResult:
    """Load every aggTrades page of one run from bronze into `target`, in ONE transaction.

    advance_watermark=False is for side loads (e.g. a reconciliation table): the
    trades land, the watermark is left exactly as it was.

    Watermark rules (it must always mean "everything up to here is loaded"):
      * moves only forward (GREATEST), never back;
      * moves only if this run's trades start at or before watermark + 1,
        otherwise there is a gap below them and the watermark stays put.
    """
    try:
        with psycopg.connect(dsn) as conn:
            with conn.transaction():  # COMMIT at the end of the block, ROLLBACK on any exception
                pages = conn.execute(
                    """
                    SELECT id, body FROM bronze.api_pages
                    WHERE run_id = %s AND endpoint = %s AND params->>'symbol' = %s
                    ORDER BY id
                    """,
                    (run_id, AGG_TRADES_PATH, symbol),
                ).fetchall()

                wm_before = _read_watermark_for_update(conn, symbol)
                if not pages:
                    logger.info(
                        "%s: run %s has no aggTrades pages in bronze; nothing to load", symbol, run_id
                    )
                    return LoadResult(run_id, symbol, 0, 0, 0, wm_before, wm_before)

                rows: list[tuple] = []
                for bronze_id, body in pages:
                    for t in parse_page(symbol, body, bronze_id):
                        rows.append(
                            (
                                t.symbol,
                                t.agg_trade_id,
                                t.price,
                                t.qty,
                                t.first_trade_id,
                                t.last_trade_id,
                                t.ts_us,
                                t.is_buyer_maker,
                                t.is_best_match,
                                SOURCE,
                                bronze_id,
                            )
                        )

                with conn.cursor() as cur:
                    cur.executemany(INSERT_SQL.format(table=sql.Identifier(*target.split("."))), rows)
                    inserted = cur.rowcount  # rows actually inserted (conflicts count 0)

                run_min = min(r[1] for r in rows)
                run_max = max(r[1] for r in rows)
                if advance_watermark:
                    wm_after = _advance_watermark(conn, symbol, wm_before, run_min, run_max)
                else:
                    wm_after = wm_before
                    logger.info("%s: side load into %s; watermark left untouched", symbol, target)

        logger.info(
            "%s: run %s -> %s pages, %s trades offered, %s new in %s; watermark %s -> %s",
            symbol,
            run_id,
            len(pages),
            len(rows),
            inserted,
            target,
            wm_before,
            wm_after,
        )
        return LoadResult(run_id, symbol, len(pages), len(rows), inserted, wm_before, wm_after)

    except psycopg.Error as exc:
        first = str(exc).splitlines()[0]
        if isinstance(exc, psycopg.errors.UndefinedTable):
            raise PermanentError(f"{first} - {DDL_HINT} and sql/ddl/02_raw_binance.sql") from exc
        if isinstance(exc, psycopg.OperationalError):
            raise TransientError(f"database unavailable: {first}") from exc
        raise PermanentError(f"database error: {first}") from exc


def _advance_watermark(
    conn: psycopg.Connection, symbol: str, wm_before: int | None, run_min: int, run_max: int
) -> int | None:
    """Apply the two watermark rules inside the caller's transaction; return the new value."""
    if wm_before is not None and run_min > wm_before + 1:
        logger.warning(
            "%s: gap - run starts at %s but watermark is %s (ids %s..%s never loaded); watermark left at %s",
            symbol,
            run_min,
            wm_before,
            wm_before + 1,
            run_min - 1,
            wm_before,
        )
        return wm_before
    if wm_before is not None and run_max <= wm_before:
        return wm_before  # replay of older data: nothing to move
    conn.execute(UPSERT_WATERMARK_SQL, (SOURCE, symbol, run_max))
    return run_max
