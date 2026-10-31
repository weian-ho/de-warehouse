"""Tests 1-2: turning raw values into correct ones (pure functions, no network, no database)."""

from decimal import Decimal

import pytest

from warehouse.load import parse_trade
from warehouse.models import AggTrade
from warehouse.transform import normalise_ts_to_us

# 2025-01-01 00:00:00 UTC, the day Binance spot files switched from ms to µs
NEW_YEAR_MS = 1_735_689_600_000
NEW_YEAR_US = 1_735_689_600_000_000


# ---- Test 1: the unit switch -------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected_us"),
    [
        pytest.param(NEW_YEAR_MS - 1, NEW_YEAR_US - 1_000, id="ms, 1 ms before 2025 -> x1000"),
        pytest.param(NEW_YEAR_MS, NEW_YEAR_US, id="ms, exactly 2025-01-01 -> x1000"),
        pytest.param(NEW_YEAR_US, NEW_YEAR_US, id="us, exactly 2025-01-01 -> unchanged"),
        pytest.param(NEW_YEAR_US + 1, NEW_YEAR_US + 1, id="us, 1 us after 2025 -> unchanged"),
    ],
)
def test_normalise_ts_to_us_handles_both_units(raw, expected_us):
    assert normalise_ts_to_us(raw) == expected_us


# ---- Test 2: money correctness -----------------------------------------------------------
def _record(**overrides):
    rec = {
        "a": 1,
        "p": "93500.10000000",
        "q": "0.00100000",
        "f": 10,
        "l": 11,
        "T": NEW_YEAR_US,
        "m": True,
        "M": True,
    }
    rec.update(overrides)
    return rec


def test_parsed_price_and_qty_are_exact_decimals():
    trade = parse_trade("BTCUSDT", _record())

    assert isinstance(trade.price, Decimal) and not isinstance(trade.price, float)
    assert isinstance(trade.qty, Decimal) and not isinstance(trade.qty, float)
    assert trade.price == Decimal("93500.10")  # exact, no binary rounding
    assert trade.qty * 3 == Decimal("0.003")  # with float: 0.001 * 3 = 0.0030000000000000005


def test_a_float_price_is_refused_on_both_layers():
    with pytest.raises(TypeError):  # parser: price must arrive as a string
        parse_trade("BTCUSDT", _record(p=93500.1))
    with pytest.raises(TypeError):  # model: AggTrade itself rejects a float
        AggTrade("BTCUSDT", 1, 93500.1, Decimal("1"), 10, 11, NEW_YEAR_US, True, True)
