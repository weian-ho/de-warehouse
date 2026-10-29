"""Land Binance REST responses in bronze.api_pages, starting from the watermark.

    meta.ingest_state ──► start id ──► BinanceClient.iter_agg_trades() ──► bronze.api_pages
       (read only)                         (T028)                       one row per page,
                                                                         committed per page

This module never parses trades and never moves the watermark: load.py (T030)
does both, in one transaction, so the watermark only advances when the data
is safely in raw.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from warehouse.client import BinanceClient
from warehouse.errors import PermanentError, TransientError

logger = logging.getLogger(__name__)

SOURCE = "binance_rest"
EXCHANGE_INFO_PATH = "/api/v3/exchangeInfo"
DDL_HINT = "run sql/ddl/01_bronze_meta.sql first"


@dataclass(frozen=True)
class ExtractResult:
    run_id: str
    symbol: str
    start_from_id: int
    pages: int
    rows: int
    last_id: int | None  # last aggregate trade id landed in bronze, None if nothing landed


# ---- small database helpers -------------------------------------------------------------
def read_watermark(conn: psycopg.Connection, symbol: str, source: str = SOURCE) -> int | None:
    """Last agg_trade_id safely loaded for this source + symbol, or None on the very first run."""
    row = conn.execute(
        "SELECT last_agg_trade_id FROM meta.ingest_state WHERE source = %s AND symbol = %s",
        (source, symbol),
    ).fetchone()
    return None if row is None else int(row[0])


def land_response(
    conn: psycopg.Connection,
    endpoint: str,
    params: dict[str, Any],
    fetched_at: datetime,
    run_id: str,
    body: Any,
) -> int:
    """INSERT one API response into bronze.api_pages; returns its bronze id. Caller commits."""
    row = conn.execute(
        """
        INSERT INTO bronze.api_pages (endpoint, params, fetched_at, run_id, body)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id
        """,
        (endpoint, Jsonb(params), fetched_at, run_id, Jsonb(body)),
    ).fetchone()
    return int(row[0])


def _classify_db_error(exc: psycopg.Error) -> Exception:
    """Turn a psycopg error into our error types (same idea as bulk.py)."""
    if isinstance(exc, psycopg.errors.UndefinedTable):
        return PermanentError(f"{str(exc).splitlines()[0]} - {DDL_HINT}")
    if isinstance(exc, psycopg.OperationalError):
        return TransientError(f"database unavailable: {str(exc).splitlines()[0]}")
    return PermanentError(f"database error: {str(exc).splitlines()[0]}")


# ---- the extract run ------------------------------------------------------------------------
def _choose_start(conn: psycopg.Connection, client: BinanceClient, symbol: str, from_id: int | None) -> int:
    """--from-id wins; else watermark + 1; else start from the newest trade ('collect from now')."""
    watermark = read_watermark(conn, symbol)
    if from_id is not None:
        if watermark is not None:
            logger.warning("%s: --from-id %s overrides watermark %s", symbol, from_id, watermark)
        return from_id
    if watermark is not None:
        logger.info("%s: watermark %s -> starting at %s", symbol, watermark, watermark + 1)
        return watermark + 1
    newest = client.latest_agg_trade_id(symbol)
    logger.info("%s: no watermark yet -> starting from the newest trade %s", symbol, newest)
    return newest


def run_extract(
    client: BinanceClient,
    dsn: str,
    symbol: str,
    run_id: str,
    from_id: int | None = None,
    max_pages: int = 100,
    stop_at_id: int | None = None,
) -> ExtractResult:
    """Page forward from the start id and land every page in bronze, one commit per page.

    A failure part-way through keeps every page already committed; the watermark is
    untouched, so the next run simply starts from the same place again.
    """
    pages = rows = 0
    last_id: int | None = None
    try:
        with psycopg.connect(dsn) as conn:
            start = _choose_start(conn, client, symbol, from_id)
            for page in client.iter_agg_trades(symbol, start, stop_at_id=stop_at_id):
                bronze_id = land_response(
                    conn, page.endpoint, page.params, page.fetched_at, run_id, page.rows
                )
                conn.commit()  # the batching is the page: this page is now safe
                pages += 1
                rows += len(page.rows)
                last_id = page.rows[-1]["a"]
                logger.debug("landed bronze id %s (%s rows)", bronze_id, len(page.rows))
                if pages >= max_pages:
                    logger.info("%s: reached max_pages=%s; stopping this run", symbol, max_pages)
                    break
    except psycopg.Error as exc:
        raise _classify_db_error(exc) from exc
    finally:
        if pages:
            logger.info("%s: landed %s pages / %s rows in bronze (run %s)", symbol, pages, rows, run_id)

    return ExtractResult(run_id, symbol, start, pages, rows, last_id)


def run_exchange_info(client: BinanceClient, dsn: str, symbols: list[str], run_id: str) -> int:
    """STRETCH: land one exchangeInfo snapshot per symbol in bronze. Returns how many landed."""
    landed = 0
    try:
        with psycopg.connect(dsn) as conn:
            for symbol in symbols:
                params = {"symbol": symbol}
                body = client.get_json(EXCHANGE_INFO_PATH, params)
                land_response(conn, EXCHANGE_INFO_PATH, params, datetime.now(UTC), run_id, body)
                conn.commit()
                landed += 1
                logger.info("%s: exchangeInfo snapshot landed in bronze", symbol)
    except psycopg.Error as exc:
        raise _classify_db_error(exc) from exc
    return landed
