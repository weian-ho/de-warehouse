"""T035: integration tests against a real, throwaway Postgres database.

Unit tests (T034) fake the outside world. These do not: they create a real
database, apply the real DDL, insert real bronze pages, and run the real
load_run(). They prove what only a database can prove: idempotency and
transactional watermarking.

Needs the Docker Postgres running. If it is not reachable, the tests are
SKIPPED (not failed), so `uv run pytest` still works without Docker.
"""

from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

import warehouse.load as load
from warehouse.client import AGG_TRADES_PATH
from warehouse.config import get_settings
from warehouse.extract import SOURCE, land_response

TEST_DB = "warehouse_test"
DDL_DIR = Path(__file__).resolve().parents[1] / "sql" / "ddl"
DDL_FILES = ["01_bronze_meta.sql", "02_raw_binance.sql"]
SYMBOL, RUN = "BTCUSDT", "fixture-run"


# ---- the fixture: create a fresh database, hand it to the test, drop it afterwards --------
@pytest.fixture
def test_dsn():
    try:
        main_dsn = get_settings().dsn.get_secret_value()
    except Exception as exc:  # no .env etc.
        pytest.skip(f"settings not available: {exc}")
    admin_dsn = make_conninfo(main_dsn, dbname="postgres")  # maintenance DB, never the real one
    test_dsn = make_conninfo(main_dsn, dbname=TEST_DB)
    assert conninfo_to_dict(test_dsn)["dbname"].endswith("_test"), "refusing to touch a non-test DB"

    try:
        # CREATE/DROP DATABASE cannot run inside a transaction -> autocommit
        admin = psycopg.connect(admin_dsn, autocommit=True)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres not reachable (is Docker up?): {str(exc).splitlines()[0]}")

    with admin:
        admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")  # leftover from a crashed run
        admin.execute(f"CREATE DATABASE {TEST_DB}")
        try:
            with psycopg.connect(test_dsn) as conn:  # apply the REAL DDL files
                for name in DDL_FILES:
                    conn.execute((DDL_DIR / name).read_text(encoding="utf-8"))
            yield test_dsn  # <- the test runs here
        finally:
            admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")


# ---- small helpers -----------------------------------------------------------------------------
def seed_bronze(dsn, first_id=100, pages=3, per_page=10):
    """Land fixture pages in bronze through the real land_response(), like extract does."""
    with psycopg.connect(dsn) as conn:
        for p in range(pages):
            start = first_id + p * per_page
            body = [
                {
                    "a": i,
                    "p": "93500.10",
                    "q": "0.00100000",
                    "f": i * 2,
                    "l": i * 2 + 1,
                    "T": 1_735_689_600_000_000 + i,
                    "m": True,
                    "M": False,
                }
                for i in range(start, start + per_page)
            ]
            land_response(
                conn,
                AGG_TRADES_PATH,
                {"symbol": SYMBOL, "fromId": start, "limit": per_page},
                datetime.now(UTC),
                RUN,
                body,
            )
    return first_id + pages * per_page - 1  # last trade id seeded


def raw_count(dsn):
    with psycopg.connect(dsn) as conn:
        return conn.execute("SELECT count(*) FROM raw.binance_agg_trades").fetchone()[0]


def watermark(dsn):
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT last_agg_trade_id FROM meta.ingest_state WHERE source = %s AND symbol = %s",
            (SOURCE, SYMBOL),
        ).fetchone()
        return None if row is None else row[0]


# ---- Test 1: idempotency ------------------------------------------------------------------------
def test_loading_the_same_pages_twice_gives_identical_counts(test_dsn):
    last_id = seed_bronze(test_dsn)  # 3 pages x 10 trades = ids 100..129

    first = load.load_run(test_dsn, RUN, SYMBOL)
    count_after_first, wm_after_first = raw_count(test_dsn), watermark(test_dsn)
    second = load.load_run(test_dsn, RUN, SYMBOL)  # the very same pages again

    assert (first.inserted, second.inserted) == (30, 0)
    assert count_after_first == raw_count(test_dsn) == 30  # identical counts
    assert wm_after_first == watermark(test_dsn) == last_id == 129  # watermark identical too


# ---- Test 2: crash recovery ---------------------------------------------------------------------
def test_crash_mid_load_commits_nothing_and_leaves_the_watermark(test_dsn, monkeypatch):
    seed_bronze(test_dsn)
    with psycopg.connect(test_dsn) as conn:  # an existing watermark to protect
        conn.execute(
            "INSERT INTO meta.ingest_state (source, symbol, last_agg_trade_id) VALUES (%s, %s, 99)",
            (SOURCE, SYMBOL),
        )

    rows_seen_at_crash = []

    def crash_before_watermark(conn, *args, **kwargs):
        # runs INSIDE load_run's transaction, after the INSERT, where the watermark update would be
        rows_seen_at_crash.append(conn.execute("SELECT count(*) FROM raw.binance_agg_trades").fetchone()[0])
        raise RuntimeError("simulated crash between INSERT and watermark update")

    monkeypatch.setattr(load, "_advance_watermark", crash_before_watermark)

    with pytest.raises(RuntimeError, match="simulated crash"):
        load.load_run(test_dsn, RUN, SYMBOL)

    assert rows_seen_at_crash == [30]  # the crash really happened partway: 30 rows were inserted...
    assert raw_count(test_dsn) == 0  # ...and none of them were committed
    assert watermark(test_dsn) == 99  # the watermark did not move
