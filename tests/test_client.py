"""Tests 3-8: BinanceClient behaviour, with HTTP faked by `responses` (no real network)."""

import pytest
import responses
from responses import matchers

from warehouse.client import AGG_TRADES_PATH, BinanceClient
from warehouse.config import HttpSettings
from warehouse.errors import Banned, PermanentError, RateLimited, TransientError

BASE = "https://api.test"                 # never contacted: responses intercepts every request
URL = BASE + AGG_TRADES_PATH
PARAMS = {"symbol": "BTCUSDT", "limit": 1}


def make_client(max_attempts=5, page_limit=1000):
    """Client whose sleeps are recorded instead of performed (so retry tests run instantly)."""
    sleeps: list[float] = []
    http = HttpSettings(max_attempts=max_attempts, retry_base_s=1.0, retry_cap_s=30.0)
    return BinanceClient(BASE, http, page_limit=page_limit, sleep=sleeps.append), sleeps


def rows(*ids):
    return [{"a": i, "p": "1.0", "q": "1.0", "f": i, "l": i, "T": 1, "m": True, "M": True} for i in ids]


# ---- Test 3: rate limits -------------------------------------------------------------------
@responses.activate
def test_429_raises_rate_limited_carrying_retry_after():
    responses.get(URL, status=429, headers={"Retry-After": "7"})
    client, _ = make_client(max_attempts=1)            # one attempt: see the raw classification

    with pytest.raises(RateLimited) as err:
        client.get_json(AGG_TRADES_PATH, PARAMS)
    assert err.value.retry_after == 7


# ---- Test 4: ban handling ------------------------------------------------------------------
@responses.activate
def test_418_raises_banned_and_never_retries():
    responses.get(URL, status=418, headers={"Retry-After": "120"})
    client, sleeps = make_client(max_attempts=5)       # retries allowed, yet none may happen

    with pytest.raises(Banned):
        client.get_json(AGG_TRADES_PATH, PARAMS)
    assert len(responses.calls) == 1                   # exactly one request reached "Binance"
    assert sleeps == []                                 # and no waiting for a retry


# ---- Test 5: classification ----------------------------------------------------------------
@pytest.mark.parametrize(
    ("status", "error"),
    [pytest.param(503, TransientError, id="503 -> TransientError"),
     pytest.param(400, PermanentError, id="400 -> PermanentError")],
)
@responses.activate
def test_status_codes_map_to_error_classes(status, error):
    responses.get(URL, status=status, json={"msg": "x"})
    client, _ = make_client(max_attempts=1)

    with pytest.raises(error):
        client.get_json(AGG_TRADES_PATH, PARAMS)


# ---- Test 6: retry loop --------------------------------------------------------------------
@responses.activate
def test_transient_failure_then_success_is_retried():
    responses.get(URL, status=503)                     # registered in order:
    responses.get(URL, json=rows(1))                   # 1st call 503, 2nd call 200
    client, sleeps = make_client()

    assert client.get_json(AGG_TRADES_PATH, PARAMS) == rows(1)
    assert len(responses.calls) == 2
    assert len(sleeps) == 1 and 0 <= sleeps[0] <= 1.0  # one backoff, within the first ceiling


# ---- Test 7: termination -------------------------------------------------------------------
@responses.activate
def test_pagination_stops_on_an_empty_page():
    q = lambda from_id: [matchers.query_param_matcher(
        {"symbol": "BTCUSDT", "fromId": str(from_id), "limit": "3"})]
    responses.get(URL, json=rows(100, 101, 102), match=q(100))   # full page -> keep going
    responses.get(URL, json=[], match=q(103))                    # empty -> caught up
    client, _ = make_client(page_limit=3)

    pages = list(client.iter_agg_trades("BTCUSDT", from_id=100))

    assert [[r["a"] for r in p.rows] for p in pages] == [[100, 101, 102]]
    assert len(responses.calls) == 2                   # asked once more, got nothing, stopped


# ---- Test 8: the infinite-loop guard -------------------------------------------------------
@responses.activate
def test_pagination_raises_when_from_id_does_not_advance():
    responses.get(URL, json=rows(95, 96, 97))          # a full page entirely BEFORE fromId=100
    responses.get(URL, status=500)                     # tripwire: a 2nd request means no guard.
    client, _ = make_client(page_limit=3)              # Without it, a missing guard would loop
                                                       # forever and HANG the test, not fail it.

    with pytest.raises(PermanentError, match="does not move past fromId=100"):
        list(client.iter_agg_trades("BTCUSDT", from_id=100))
    assert len(responses.calls) == 1                   # refused at once, did not loop