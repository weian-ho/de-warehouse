"""T056: tests for the SCD2 loader (src/warehouse/scd2.py) on a real, throwaway Postgres database.

Each test lands hand-made exchangeInfo snapshots in bronze, applies them with apply_snapshot(),
and checks gold.dim_instrument. Tests 1-6 are the plan's CORE list; 7-8 are STRETCH.

Needs Postgres (make up). If it can't be reached, every test here is SKIPPED, not failed:
run `uv run pytest tests/test_scd2.py -v -rs` to see the reason.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.rows import namedtuple_row

from warehouse import scd2
from warehouse.config import get_settings
from warehouse.errors import PermanentError
from warehouse.extract import EXCHANGE_INFO_PATH

ROOT = Path(__file__).resolve().parents[1]
TEST_DB = "warehouse_scd2_test"  # its own database: never the real one, never test_integration's
DDL_FILES = sorted((ROOT / "sql" / "ddl").glob("*.sql")) + sorted((ROOT / "sql" / "gold").glob("*.sql"))

T1 = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)  # observation times of the hand-made snapshots
T2 = datetime(2026, 1, 2, 9, 0, tzinfo=UTC)
T3 = datetime(2026, 1, 3, 9, 0, tzinfo=UTC)


# ---------------------------------------------------------------- fixtures: a real database
@pytest.fixture(scope="module")
def test_dsn():
    """Create warehouse_scd2_test once per module with the A1 DDL + gold DDL; drop it afterwards."""
    assert TEST_DB.endswith("_test")  # safety: this fixture DROPs the database it names
    try:
        base = get_settings().dsn.get_secret_value()
    except Exception as exc:  # pydantic ValidationError when there is no .env (e.g. in CI)
        pytest.skip(f"no database settings ({type(exc).__name__}) - copy .env.example to .env")
    admin = make_conninfo(base, dbname="postgres")
    try:
        with psycopg.connect(admin, autocommit=True, connect_timeout=3) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
            conn.execute(f"CREATE DATABASE {TEST_DB}")
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres not reachable ({str(exc).splitlines()[0]}) - is Docker up? run: make up")

    dsn = make_conninfo(base, dbname=TEST_DB)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("SET client_min_messages = warning")
        for f in DDL_FILES:  # same files, same order as `make up` + `make gold`
            conn.execute(f.read_text(encoding="utf-8"))
    yield dsn

    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")


@pytest.fixture
def conn(test_dsn):
    """A clean slate per test: no snapshots, no watermark, only the Unknown (-1) rows in gold.

    autocommit=True matters: apply_snapshot() opens conn.transaction(), which is a real
    BEGIN/COMMIT only when no transaction is already open (otherwise it is a savepoint).
    """
    with psycopg.connect(test_dsn, autocommit=True) as c:
        c.execute("SET TIME ZONE 'UTC'")
        c.execute("DELETE FROM gold.dim_instrument WHERE instrument_key > 0")
        c.execute("DELETE FROM gold.dim_asset WHERE asset_key > 0")
        c.execute("TRUNCATE meta.snapshot_state, bronze.api_pages RESTART IDENTITY")
        yield c


# ---------------------------------------------------------------- helpers: build the input
def sym(symbol: str, base: str, quote: str, tick: str = "0.01000000", status: str = "TRADING") -> dict:
    """One symbol in Binance exchangeInfo shape (only the fields the loader reads)."""
    return {
        "symbol": symbol,
        "status": status,
        "baseAsset": base,
        "quoteAsset": quote,
        "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": tick},
            {"filterType": "LOT_SIZE", "stepSize": "0.00001000"},
            {"filterType": "NOTIONAL", "minNotional": "5.00000000"},
        ],
    }


BTC_A = sym("BTCUSDT", "BTC", "USDT", tick="0.10000000")
BTC_B = sym("BTCUSDT", "BTC", "USDT", tick="0.01000000")  # the tracked attribute changes
ETH = sym("ETHUSDT", "ETH", "USDT")


def land(conn: psycopg.Connection, observed_at: datetime, symbols: list[dict]) -> int:
    """Put one exchangeInfo snapshot in bronze, as `warehouse exchangeinfo` would. Returns its page id."""
    body = json.dumps({"serverTime": 0, "symbols": symbols})
    row = conn.execute(
        "INSERT INTO bronze.api_pages (endpoint, params, fetched_at, run_id, body) "
        "VALUES (%s, '{}', %s, 'test', %s) RETURNING id",
        (EXCHANGE_INFO_PATH, observed_at, body),
    ).fetchone()
    return row[0]


def versions(conn: psycopg.Connection, symbol: str) -> list:
    """All versions of one instrument, oldest first, as named tuples.

    psycopg cannot turn Postgres '-infinity' / 'infinity' into a Python datetime (it raises
    DataError), so open ends come back as None: valid_from None = -infinity, valid_to None = infinity.
    """
    with conn.cursor(row_factory=namedtuple_row) as cur:
        return cur.execute(
            """SELECT instrument_key AS key, tick_size,
                      CASE WHEN isfinite(valid_from) THEN valid_from END AS valid_from,
                      CASE WHEN isfinite(valid_to)   THEN valid_to   END AS valid_to,
                      is_current, is_inferred
               FROM gold.dim_instrument d WHERE symbol = %s
               ORDER BY d.valid_from""",  # d.: the real column, not the CASE alias above
            (symbol,),
        ).fetchall()


def dimension_state(conn: psycopg.Connection) -> list[tuple]:
    """Every row with its xmin (the id of the transaction that last wrote it).

    If a run inserts OR updates any row, this changes, even when the values written are the same.
    That is how 'zero rows inserted or updated' is checked, not just 'same values'.
    """
    return conn.execute(
        "SELECT xmin::text, d::text FROM gold.dim_instrument d ORDER BY instrument_key"
    ).fetchall()


def as_of(conn: psycopg.Connection, symbol: str, t: datetime) -> list[int]:
    """The point-in-time lookup a fact load uses: half-open [valid_from, valid_to)."""
    rows = conn.execute(
        """SELECT instrument_key FROM gold.dim_instrument
           WHERE symbol = %s AND valid_from <= %s AND %s < valid_to""",
        (symbol, t, t),
    ).fetchall()
    return [r[0] for r in rows]


# ---------------------------------------------------------------- CORE tests 1-6
def test_1_initial_load_gives_one_current_row_per_key(conn):
    scd2.apply_snapshot(conn, land(conn, T1, [BTC_A, ETH]))

    for symbol in ("BTCUSDT", "ETHUSDT"):
        rows = versions(conn, symbol)
        assert len(rows) == 1, f"{symbol}: expected 1 version, got {len(rows)}"
        v = rows[0]
        assert v.is_current
        # D3: the first version reaches back to -infinity so backfilled trades find it,
        # and is flagged as assumed history.
        assert v.valid_from is None and v.is_inferred
        assert v.valid_to is None  # 'infinity': still open


def test_2_change_closes_old_row_at_new_valid_from_and_opens_new(conn):
    scd2.apply_snapshot(conn, land(conn, T1, [BTC_A, ETH]))
    result = scd2.apply_snapshot(conn, land(conn, T2, [BTC_B, ETH]))

    assert (result.new, result.changed, result.unchanged) == (0, 1, 1)
    old, new = versions(conn, "BTCUSDT")
    # the old row is closed exactly where the new one starts: no gap, no overlap
    assert old.valid_to == new.valid_from == T2, (
        f"old.valid_to={old.valid_to} new.valid_from={new.valid_from}"
    )
    assert not old.is_current and new.is_current
    assert new.valid_to is None  # open
    assert (old.tick_size, new.tick_size) == (Decimal("0.1"), Decimal("0.01"))
    assert not new.is_inferred  # observed, not assumed
    assert len(versions(conn, "ETHUSDT")) == 1  # unchanged key: untouched


def test_3_rerunning_changes_nothing(conn):
    page = land(conn, T1, [BTC_A, ETH])
    scd2.apply_snapshot(conn, page)
    before = dimension_state(conn)

    # (a) the SAME snapshot again: an expected replay -> skipped
    replay = scd2.apply_snapshot(conn, page)
    assert replay.skipped
    assert dimension_state(conn) == before

    # (b) a NEWER snapshot with the same values (Binance formats "0.1" differently this time):
    #     hash-gated, so no new version and not even an UPDATE of the existing rows
    same = [sym("BTCUSDT", "BTC", "USDT", tick="0.1"), ETH]
    result = scd2.apply_snapshot(conn, land(conn, T2, same))
    assert (result.new, result.changed, result.unchanged) == (0, 0, 2)
    assert dimension_state(conn) == before


def test_4_current_rows_equal_distinct_keys(conn):
    # a busier history: change, status flip and back (A -> B -> A), a new symbol
    scd2.apply_snapshot(conn, land(conn, T1, [BTC_A, ETH]))
    scd2.apply_snapshot(conn, land(conn, T2, [BTC_B, sym("ETHUSDT", "ETH", "USDT", status="BREAK")]))
    scd2.apply_snapshot(conn, land(conn, T3, [BTC_B, ETH, sym("SOLUSDT", "SOL", "USDT")]))

    keys, current = conn.execute(
        """SELECT count(DISTINCT (venue_key, symbol)), count(*) FILTER (WHERE is_current)
           FROM gold.dim_instrument WHERE instrument_key > 0"""
    ).fetchone()
    assert (keys, current) == (3, 3)
    assert len(versions(conn, "ETHUSDT")) == 3  # TRADING -> BREAK -> TRADING is three periods, not one


def test_5_as_of_lookup_at_the_boundary(conn):
    scd2.apply_snapshot(conn, land(conn, T1, [BTC_A, ETH]))
    scd2.apply_snapshot(conn, land(conn, T2, [BTC_B, ETH]))
    old_key, new_key = (v.key for v in versions(conn, "BTCUSDT"))

    assert as_of(conn, "BTCUSDT", T2) == [new_key]  # [valid_from: the start is included
    assert as_of(conn, "BTCUSDT", T2 - timedelta(microseconds=1)) == [old_key]  # valid_to): end excluded
    assert as_of(conn, "BTCUSDT", datetime(2024, 12, 31, tzinfo=UTC)) == [old_key]  # backfill era: D3


def test_6_older_snapshot_is_refused_and_dimension_unchanged(conn):
    scd2.apply_snapshot(conn, land(conn, T1, [BTC_A, ETH]))
    scd2.apply_snapshot(conn, land(conn, T3, [BTC_B, ETH]))
    before = dimension_state(conn)
    watermark = conn.execute("SELECT last_page_id, last_fetched_at FROM meta.snapshot_state").fetchall()

    late = land(conn, T2, [BTC_A, ETH])  # landed last, but observed BEFORE the T3 snapshot
    with pytest.raises(PermanentError, match="not newer than the last one applied"):
        scd2.apply_snapshot(conn, late)

    assert dimension_state(conn) == before
    assert (
        conn.execute("SELECT last_page_id, last_fetched_at FROM meta.snapshot_state").fetchall() == watermark
    )


# ---------------------------------------------------------------- STRETCH
def test_7_overlapping_version_is_rejected_by_the_exclusion_constraint(conn):
    """The last line of defence: even SQL that bypasses the loader cannot create overlapping history."""
    scd2.apply_snapshot(conn, land(conn, T1, [BTC_A]))
    with pytest.raises(psycopg.errors.ExclusionViolation, match="dim_instrument_no_overlap"):
        conn.execute(
            """INSERT INTO gold.dim_instrument (venue_key, symbol, base_asset_key, quote_asset_key, status,
                                                valid_from, is_current, attr_hash)
               SELECT venue_key, symbol, base_asset_key, quote_asset_key, 'TRADING', %s, true, 'x'
               FROM gold.dim_instrument WHERE symbol = 'BTCUSDT'""",
            (T2,),  # forgot to close the current row first
        )


def test_8_crash_between_close_and_open_rolls_back_everything(conn, monkeypatch):
    """Atomicity: the close step already ran when the open step fails, and must be undone with it."""
    scd2.apply_snapshot(conn, land(conn, T1, [BTC_A, ETH]))
    before = dimension_state(conn)
    watermark = conn.execute("SELECT * FROM meta.snapshot_state").fetchall()

    monkeypatch.setattr(scd2, "OPEN_SQL", "SELECT 1/0")  # the open step now fails
    with pytest.raises(psycopg.errors.DivisionByZero):
        scd2.apply_snapshot(conn, land(conn, T2, [BTC_B, ETH]))

    assert dimension_state(conn) == before  # BTCUSDT still has its current row
    assert conn.execute("SELECT * FROM meta.snapshot_state").fetchall() == watermark
