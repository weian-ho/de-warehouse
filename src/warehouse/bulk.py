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
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import IO

import psycopg
from psycopg import sql

from warehouse.errors import PermanentError, TransientError

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
