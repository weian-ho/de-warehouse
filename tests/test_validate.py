"""Tests 9-10: data-quality verdicts (pure functions, no database)."""

from warehouse.validate import FAIL, PASS, check_schema, check_volume

TARGET = "BTCUSDT/aggTrades"


def good(i):
    return {"a": i, "p": "1.0", "q": "1.0", "f": i, "l": i, "T": 1, "m": True, "M": True}


# ---- Test 9: completeness ------------------------------------------------------------------
def test_volume_fails_when_one_id_is_missing():
    # ids 1..1000 should be 1000 rows; one is missing
    result = check_volume(TARGET, loaded_rows=999, first_id=1, last_id=1000)

    assert result.status == FAIL
    assert "1 missing" in result.expected


def test_volume_passes_when_every_id_is_present():
    assert check_volume(TARGET, loaded_rows=1000, first_id=1, last_id=1000).status == PASS


# ---- Test 10: schema drift -----------------------------------------------------------------
def test_schema_fails_when_a_key_is_missing():
    page = [good(1), good(2), good(3)]
    del page[1]["T"]                                   # Binance "drops" a field in one record

    result = check_schema(TARGET, [(42, page)])

    assert result.status == FAIL
    assert "1 of 3 records bad" in result.observed
    assert "bronze page 42 record 1" in result.observed and "'T'" in result.observed


def test_schema_passes_on_a_clean_page():
    assert check_schema(TARGET, [(42, [good(1), good(2)])]).status == PASS