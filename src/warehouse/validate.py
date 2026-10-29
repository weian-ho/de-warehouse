"""Data-quality checks for A1 ingestion: freshness, volume, schema.

Each check returns a verdict (CheckResult), never rows:

    gather (database, validate_run)            decide (pure functions, no I/O)
    ───────────────────────────────            ───────────────────────────────
    watermark + window's last id        ──►    check_freshness()  ─┐
    rows loaded inside the window       ──►    check_volume()      ├─► list[CheckResult]
    the run's bronze records            ──►    check_schema()     ─┘

The window is one ingest run: from the first fromId it requested to the last
trade id it landed in bronze.
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import psycopg

from warehouse.client import AGG_TRADES_PATH
from warehouse.errors import PermanentError, TransientError
from warehouse.extract import DDL_HINT, SOURCE

logger = logging.getLogger(__name__)

PASS, FAIL = "PASS", "FAIL"

# aggTrades key -> the Python type json/jsonb gives us for it
EXPECTED_TYPES: dict[str, type] = {
    "a": int,
    "p": str,
    "q": str,
    "f": int,
    "l": int,
    "T": int,
    "m": bool,
    "M": bool,
}


@dataclass(frozen=True)
class CheckResult:
    check: str  # "freshness" | "volume" | "schema"
    target: str  # "BTCUSDT/aggTrades"
    status: str  # "PASS" | "FAIL"
    observed: str
    expected: str


# ---- decide: pure functions, no database --------------------------------------------------
def check_freshness(target: str, watermark: int | None, window_last_id: int) -> CheckResult:
    """PASS if the watermark has reached the last trade id of the window."""
    ok = watermark is not None and watermark >= window_last_id
    return CheckResult(
        "freshness",
        target,
        PASS if ok else FAIL,
        observed=f"watermark {watermark}",
        expected=f">= {window_last_id}",
    )


def check_volume(target: str, loaded_rows: int, first_id: int, last_id: int) -> CheckResult:
    """PASS if every id in [first_id, last_id] is loaded. Zero tolerance: ids are gapless."""
    expected = last_id - first_id + 1
    diff = expected - loaded_rows
    note = f" ({diff} missing)" if diff > 0 else f" ({-diff} extra)" if diff < 0 else ""
    return CheckResult(
        "volume",
        target,
        PASS if diff == 0 else FAIL,
        observed=f"{loaded_rows} rows in ids {first_id}..{last_id}",
        expected=f"{expected} rows{note}",
    )


def _type_ok(value: Any, expected: type) -> bool:
    # bool is a subclass of int in Python: isinstance(True, int) is True, so exclude it
    if expected is int:
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, expected)


def record_problem(rec: Any) -> str | None:
    """None if one record matches the aggTrades schema, else a short description."""
    if not isinstance(rec, dict):
        return f"not a JSON object ({type(rec).__name__})"
    missing = [k for k in EXPECTED_TYPES if k not in rec]
    if missing:
        return f"missing keys {missing}"
    wrong = [f"{k}={type(rec[k]).__name__}" for k, t in EXPECTED_TYPES.items() if not _type_ok(rec[k], t)]
    if wrong:
        return f"wrong types {wrong}"
    return None


def check_schema(target: str, pages: Iterable[tuple[int, Any]]) -> CheckResult:
    """PASS if every record of every page has all aggTrades keys with the expected types."""
    total = bad = 0
    first: str | None = None
    for bronze_id, body in pages:
        if not isinstance(body, list):
            total, bad = total + 1, bad + 1
            first = first or f"bronze page {bronze_id}: body is not a JSON list"
            continue
        for i, rec in enumerate(body):
            total += 1
            problem = record_problem(rec)
            if problem:
                bad += 1
                first = first or f"bronze page {bronze_id} record {i}: {problem}"
    observed = f"{bad} of {total} records bad" + (f"; first: {first}" if first else "")
    return CheckResult(
        "schema",
        target,
        PASS if bad == 0 and total > 0 else FAIL,
        observed=observed,
        expected="0 bad (keys a p q f l T m M, typed)",
    )


# ---- gather: read the database, then call the pure checks ---------------------------------
def _latest_run(conn: psycopg.Connection, symbol: str) -> str:
    row = conn.execute(
        """SELECT run_id FROM bronze.api_pages
           WHERE endpoint = %s AND params->>'symbol' = %s
           GROUP BY run_id ORDER BY max(id) DESC LIMIT 1""",
        (AGG_TRADES_PATH, symbol),
    ).fetchone()
    if row is None:
        raise PermanentError(f"no aggTrades runs for {symbol} in bronze - run ingest first")
    return row[0]


def validate_run(dsn: str, symbol: str, run_id: str | None = None) -> tuple[str, list[CheckResult]]:
    """Run the three checks for one ingest run (default: the latest). Returns (run_id, results)."""
    target = f"{symbol}/aggTrades"
    try:
        with psycopg.connect(dsn) as conn:
            run_id = run_id or _latest_run(conn, symbol)
            pages = conn.execute(
                """SELECT id, (params->>'fromId')::bigint, body FROM bronze.api_pages
                   WHERE run_id = %s AND endpoint = %s AND params->>'symbol' = %s ORDER BY id""",
                (run_id, AGG_TRADES_PATH, symbol),
            ).fetchall()
            if not pages:
                raise PermanentError(f"run {run_id} has no aggTrades pages for {symbol} in bronze")

            # the window: first requested fromId .. highest trade id actually landed
            first_id = min(from_id for _, from_id, _ in pages)
            ids = [
                r["a"]
                for _, _, body in pages
                if isinstance(body, list)
                for r in body
                if isinstance(r, dict) and _type_ok(r.get("a"), int)
            ]
            last_id = max(ids) if ids else first_id - 1

            wm_row = conn.execute(
                "SELECT last_agg_trade_id FROM meta.ingest_state WHERE source = %s AND symbol = %s",
                (SOURCE, symbol),
            ).fetchone()
            loaded = conn.execute(
                """SELECT count(*) FROM raw.binance_agg_trades
                   WHERE symbol = %s AND agg_trade_id BETWEEN %s AND %s""",  # PK range scan
                (symbol, first_id, last_id),
            ).fetchone()[0]
    except psycopg.Error as exc:
        first_line = str(exc).splitlines()[0]
        if isinstance(exc, psycopg.errors.UndefinedTable):
            raise PermanentError(f"{first_line} - {DDL_HINT} and sql/ddl/02_raw_binance.sql") from exc
        if isinstance(exc, psycopg.OperationalError):
            raise TransientError(f"database unavailable: {first_line}") from exc
        raise PermanentError(f"database error: {first_line}") from exc

    results = [
        check_freshness(target, None if wm_row is None else int(wm_row[0]), last_id),
        check_volume(target, loaded, first_id, last_id),
        check_schema(target, ((bronze_id, body) for bronze_id, _, body in pages)),
    ]
    n_fail = sum(r.status == FAIL for r in results)
    logger.info(
        "%s: run %s -> %s checks, %s PASS, %s FAIL",
        target,
        run_id,
        len(results),
        len(results) - n_fail,
        n_fail,
    )
    return run_id, results


def format_summary(run_id: str, results: list[CheckResult]) -> str:
    """Plain-text table, one row per check."""
    header = ("check", "status", "observed", "expected")
    rows = [(r.check, r.status, r.observed, r.expected) for r in results]
    widths = [max(len(str(x)) for x in col) for col in zip(header, *rows, strict=True)]

    def line(cells) -> str:
        return "  ".join(str(c).ljust(w) for c, w in zip(cells, widths, strict=True)).rstrip()

    target = results[0].target if results else "?"
    return "\n".join(
        [
            f"validate {target} - run {run_id}",
            line(header),
            line(["-" * w for w in widths]),
            *(line(r) for r in rows),
        ]
    )
