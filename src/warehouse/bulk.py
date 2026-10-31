"""Load one already-downloaded Binance daily aggTrades zip into Postgres.

Python port of the load() function in sql/analytical/load_binance.sh.
Same behaviour, same tables, same result:

    bash                                         python (this file)
    psql -c "TRUNCATE stage"                     TRUNCATE stage            -+
    unzip -p | awk (drop header) | \\copy         stream zip -> COPY stage   |  ONE transaction
    INSERT INTO target                           INSERT INTO target        -+
      SELECT sym, s.*, csv, batch FROM stage       SELECT sym, s.*, csv, batch FROM stage

Timestamps are loaded exactly as Binance sent them (ts_raw), like the bash
version: the raw layer keeps the source value. Normalising ms -> us happens
later (transform.normalise_ts_to_us, used from T030).
"""

import csv
import io
import logging
import time
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import InvalidOperation
from itertools import islice
from pathlib import Path
from typing import IO

import psycopg
from psycopg import sql

from warehouse.errors import PermanentError, TransientError
from warehouse.load import DEFAULT_TARGET, INSERT_SQL, parse_trade
from warehouse.transform import parse_bool

logger = logging.getLogger(__name__)

N_FIELDS = 8  # agg_trade_id, price, qty, first_id, last_id, ts, is_buyer_maker, is_best_match


def _table(name: str) -> sql.Identifier:
    """'raw.agg_trades' -> safely quoted "raw"."agg_trades"."""
    return sql.Identifier(*name.split("."))


def symbol_from_filename(path: Path) -> str:
    """'BTCUSDT-aggTrades-2025-01-01.zip' -> 'BTCUSDT'."""
    symbol, kind, *_ = path.stem.split("-")
    if kind != "aggTrades":
        raise PermanentError(f"not an aggTrades file: {path.name}")
    return symbol


def iter_csv_rows(raw: IO[bytes], source: str) -> Iterator[list[str | None]]:
    """Yield CSV rows one at a time (memory stays flat).

    Same rules as the bash version:
      * drop line 1 only if it does not start with a digit (the awk line)
      * an empty field becomes NULL, as `\\copy ... WITH (FORMAT csv)` does
    """
    reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8", newline=""))
    for line_no, rec in enumerate(reader, start=1):
        if line_no == 1 and not rec[0][:1].isdigit():
            logger.info("%s: header row detected, skipping", source)
            continue
        if len(rec) != N_FIELDS:
            raise PermanentError(f"{source} line {line_no}: expected {N_FIELDS} fields, got {len(rec)}")
        yield [value if value != "" else None for value in rec]


def _copy_and_insert(
    zf: zipfile.ZipFile, symbol: str, dsn: str, batch_id: int, target: str, stage: str
) -> tuple[str, int, int]:
    """TRUNCATE stage, COPY the CSV into it, INSERT into target. Returns (csv_name, copied, inserted)."""
    csv_name = zf.namelist()[0]  # bash: unzip -Z1 "$zip" | head -1

    # `with connect(...)`: COMMIT if the block finishes, ROLLBACK on any exception.
    # So TRUNCATE + COPY + INSERT succeed together or not at all.
    with psycopg.connect(dsn) as conn, conn.cursor() as cur, zf.open(csv_name) as raw:
        cur.execute(sql.SQL("TRUNCATE {}").format(_table(stage)))

        copied = 0
        with cur.copy(sql.SQL("COPY {} FROM STDIN").format(_table(stage))) as copy:
            for rec in iter_csv_rows(raw, csv_name):
                copy.write_row(rec)
                copied += 1

        cur.execute(
            sql.SQL("INSERT INTO {} SELECT %s, s.*, %s, %s FROM {} s").format(_table(target), _table(stage)),
            (symbol, csv_name, batch_id),
        )
        inserted = cur.rowcount

    return csv_name, copied, inserted


def load_zip(
    zip_path: Path,
    dsn: str,
    batch_id: int = 1,
    target: str = "raw.agg_trades",
    stage: str = "raw.agg_trades_stage",
) -> int:
    """Load one zip via the stage table, in a single transaction. Returns rows inserted."""
    symbol = symbol_from_filename(zip_path)

    try:
        with zipfile.ZipFile(zip_path) as zf:
            csv_name, copied, inserted = _copy_and_insert(zf, symbol, dsn, batch_id, target, stage)
    except psycopg.errors.UndefinedTable as exc:
        first_line = str(exc).splitlines()[0]  # e.g. relation "raw.x" does not exist
        raise PermanentError(
            f"{first_line} - run sql/analytical/00_setup.sql or check --table/--stage"
        ) from exc
    except (psycopg.DataError, psycopg.IntegrityError) as exc:
        # DataError:      bad value, e.g. 'maybe' in a boolean column, or a number too large
        # IntegrityError: a constraint broke, e.g. NULL in a NOT NULL column
        # Either way the file is bad and retrying won't help.
        raise PermanentError(f"{zip_path.name}: Postgres rejected the data: {exc}") from exc
    except psycopg.OperationalError as exc:
        # database down / connection dropped: may work if we try again later
        raise TransientError(f"database unavailable: {exc}") from exc

    if inserted != copied:  # cheap self-check: stage and target must agree
        raise PermanentError(f"{csv_name}: copied {copied} rows but inserted {inserted}")
    logger.info("loaded %s rows from %s into %s (batch %s)", inserted, csv_name, target, batch_id)
    return inserted


BULK_SOURCE = "binance_bulk"
CHUNK_ROWS = 50_000  # rows per executemany: keeps memory flat on million-row days


@dataclass(frozen=True)
class BackfillDay:
    day: date
    file: str
    offered: int
    inserted: int


def day_files(data_dir: Path, symbol: str, start: date, end: date) -> list[tuple[date, Path]]:
    """Every day in [start, end] (inclusive) with its zip. Refuses up front if any file is missing."""
    if end < start:
        raise PermanentError(f"--end {end} is before --start {start}")
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    files = [(d, data_dir / f"{symbol}-aggTrades-{d.isoformat()}.zip") for d in days]
    missing = [p.name for _, p in files if not p.is_file()]
    if missing:
        raise PermanentError(
            f"{len(missing)} file(s) missing in {data_dir}: {', '.join(missing)} "
            "- refusing to start, a skipped day would be a silent gap"
        )
    return files


def iter_raw_rows(zip_path: Path, symbol: str) -> Iterator[tuple]:
    """Zip -> CSV rows -> AggTrade (same parse_trade as REST) -> tuples in INSERT_SQL column order."""
    with zipfile.ZipFile(zip_path) as zf:
        csv_name = zf.namelist()[0]
        with zf.open(csv_name) as raw:
            for n, rec in enumerate(iter_csv_rows(raw, csv_name), start=1):
                agg_id, price, qty, first_id, last_id, ts, maker, best = rec
                try:
                    t = parse_trade(
                        symbol,
                        {
                            "a": int(agg_id),
                            "p": price,
                            "q": qty,
                            "f": int(first_id),
                            "l": int(last_id),
                            "T": int(ts),
                            "m": parse_bool(maker),
                            "M": parse_bool(best),
                        },
                    )
                except (KeyError, ValueError, TypeError, InvalidOperation) as exc:
                    raise PermanentError(f"{csv_name} data row {n}: {exc}") from exc
                yield (
                    t.symbol,
                    t.agg_trade_id,
                    t.price,
                    t.qty,
                    t.first_trade_id,
                    t.last_trade_id,
                    t.ts_us,
                    t.is_buyer_maker,
                    t.is_best_match,
                    BULK_SOURCE,
                    None,
                )


def _chunks(rows: Iterator[tuple], size: int) -> Iterator[list[tuple]]:
    while chunk := list(islice(rows, size)):
        yield chunk


def backfill_day(conn: psycopg.Connection, zip_path: Path, symbol: str) -> tuple[int, int]:
    """Load one day in ONE transaction: the whole day commits, or none of it. Returns (offered, inserted)."""
    stmt = INSERT_SQL.format(table=_table(DEFAULT_TARGET))
    offered = inserted = 0
    with conn.transaction(), conn.cursor() as cur:
        for chunk in _chunks(iter_raw_rows(zip_path, symbol), CHUNK_ROWS):
            cur.executemany(stmt, chunk)
            offered += len(chunk)
            inserted += cur.rowcount  # conflicts (already-loaded trades) count 0
    return offered, inserted


def run_backfill(dsn: str, symbol: str, start: date, end: date, data_dir: Path) -> list[BackfillDay]:
    """Backfill [start, end] day by day. Never touches the watermark: that belongs to the REST tail."""
    files = day_files(data_dir, symbol, start, end)  # every file present, or nothing starts
    results: list[BackfillDay] = []
    try:
        with psycopg.connect(dsn) as conn:
            for day, path in files:
                t0 = time.monotonic()
                offered, inserted = backfill_day(conn, path, symbol)
                results.append(BackfillDay(day, path.name, offered, inserted))
                logger.info(
                    "%s %s: %s rows offered, %s new in %s (%.1fs)",
                    symbol,
                    day,
                    offered,
                    inserted,
                    DEFAULT_TARGET,
                    time.monotonic() - t0,
                )
    except psycopg.Error as exc:
        first = str(exc).splitlines()[0]
        if isinstance(exc, psycopg.errors.UndefinedTable):
            raise PermanentError(f"{first} - run sql/ddl/02_raw_binance.sql") from exc
        if isinstance(exc, psycopg.OperationalError):
            raise TransientError(f"database unavailable: {first}") from exc
        raise PermanentError(f"database error: {first}") from exc
    finally:
        if results:
            logger.info(
                "%s backfill: %s of %s days done, %s rows offered, %s new",
                symbol,
                len(results),
                len(files),
                sum(r.offered for r in results),
                sum(r.inserted for r in results),
            )
    return results
