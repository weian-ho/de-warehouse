"""HTTP client for Binance public market data (https://data-api.binance.vision).

Three layers, each built on the one below:

    iter_agg_trades()   pages through aggTrades with the fromId cursor (a generator)
          |
    get_json()          one logical request: retries 429 and transient failures
          |
    _send_once()        one HTTP attempt: sends it, classifies the answer, never retries

No database and no parsing here: callers receive raw JSON pages, exactly as
Binance sent them, so extract.py can land them in bronze before anything else.
"""

import logging
import random
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import requests

from warehouse.config import HttpSettings
from warehouse.errors import Banned, PermanentError, RateLimited, TransientError

logger = logging.getLogger(__name__)

AGG_TRADES_PATH = "/api/v3/aggTrades"
MAX_PAGE_LIMIT = 1000  # Binance's maximum for aggTrades
DEFAULT_RETRY_AFTER_S = 60  # used only if a 429/418 arrives without a Retry-After header


@dataclass(frozen=True)
class Page:
    """One aggTrades response, untouched, plus what is needed to land it in bronze."""

    endpoint: str
    params: dict[str, Any]
    rows: list[dict[str, Any]]
    fetched_at: datetime


def _retry_after_seconds(resp: requests.Response) -> int:
    """Read the Retry-After header (whole seconds); fall back to a safe default."""
    value = resp.headers.get("Retry-After")
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return DEFAULT_RETRY_AFTER_S


class BinanceClient:
    """One requests.Session for the whole run; use it as `with BinanceClient(...) as client:`."""

    def __init__(
        self,
        base_url: str,
        http: HttpSettings,
        page_limit: int = MAX_PAGE_LIMIT,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = http
        self.page_limit = min(page_limit, MAX_PAGE_LIMIT)
        self._sleep = sleep  # injectable: tests pass a fake so retries don't really wait
        self.last_used_weight: str | None = None  # X-MBX-USED-WEIGHT-1M of the latest response

        self.session = requests.Session()
        self.session.headers.update(
            {
                "X-MBX-TIME-UNIT": "MICROSECOND",  # timestamps in µs, like the post-2025 files
                "User-Agent": "de-warehouse/0.1",
            }
        )

    # ---- context manager: closes the network connection when the block ends ----
    def __enter__(self) -> "BinanceClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.session.close()

    # ---- layer 1: one HTTP attempt ------------------------------------------------
    def _send_once(self, path: str, params: dict[str, Any]) -> Any:
        """Send one request and classify the outcome. Never retries.

        200            -> parsed JSON
        429            -> RateLimited(retry_after)
        418            -> Banned(retry_after)
        5xx            -> TransientError
        other 4xx      -> PermanentError
        timeout / connection failure -> TransientError
        """
        try:
            resp = self.session.get(
                self.base_url + path,
                params=params,
                timeout=(self.http.connect_timeout_s, self.http.read_timeout_s),
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise TransientError(f"{type(exc).__name__} calling {path}") from exc

        self.last_used_weight = resp.headers.get("X-MBX-USED-WEIGHT-1M")
        status = resp.status_code
        logger.debug("GET %s %s -> HTTP %s (used weight %s)", path, params, status, self.last_used_weight)

        if status == 200:
            try:
                return resp.json()
            except ValueError as exc:  # cut-off or garbled body: asking again may work
                raise TransientError(f"invalid JSON from {path}") from exc
        if status == 429:
            raise RateLimited(retry_after=_retry_after_seconds(resp))
        if status == 418:
            raise Banned(retry_after=_retry_after_seconds(resp))
        if status >= 500:
            raise TransientError(f"HTTP {status} from {path}")
        raise PermanentError(f"HTTP {status} from {path}: {resp.text[:200]}")

    # ---- layer 2: one logical request, with retries ----------------------------------
    def _backoff_seconds(self, attempt: int) -> float:
        """Capped exponential backoff with full jitter: random between 0 and base * 2^(attempt-1)."""
        ceiling = min(self.http.retry_cap_s, self.http.retry_base_s * 2 ** (attempt - 1))
        return random.uniform(0, ceiling)

    def get_json(self, path: str, params: dict[str, Any]) -> Any:
        """Call _send_once until it succeeds or attempts run out.

        RateLimited    -> wait exactly Retry-After, then try again
        TransientError -> wait backoff with jitter, then try again
        Banned, PermanentError -> not caught here: they stop the caller immediately
        Every retry, of either kind, uses up one of max_attempts.
        """
        max_attempts = self.http.max_attempts
        for attempt in range(1, max_attempts + 1):
            try:
                return self._send_once(path, params)
            except RateLimited as exc:
                if attempt == max_attempts:
                    raise
                wait = float(exc.retry_after)
                logger.warning(
                    "429 rate limited; waiting Retry-After=%ss (attempt %s/%s)",
                    exc.retry_after,
                    attempt,
                    max_attempts,
                )
            except TransientError as exc:
                if attempt == max_attempts:
                    raise
                wait = self._backoff_seconds(attempt)
                logger.warning("%s; retrying in %.2fs (attempt %s/%s)", exc, wait, attempt, max_attempts)
            self._sleep(wait)
        raise AssertionError("unreachable: the loop always returns or raises")

    # ---- layer 3: pagination ---------------------------------------------------------
    def latest_agg_trade_id(self, symbol: str) -> int:
        """Id of the most recent aggregate trade (no fromId -> Binance returns the newest)."""
        rows = self.get_json(AGG_TRADES_PATH, {"symbol": symbol, "limit": 1})
        if not rows:
            raise PermanentError(f"no aggTrades returned for {symbol}")
        return int(rows[0]["a"])

    def iter_agg_trades(self, symbol: str, from_id: int, stop_at_id: int | None = None) -> Iterator[Page]:
        """Yield aggTrades pages from from_id (inclusive) onward, one page per request.

        Stops when: a page is empty or shorter than page_limit (caught up with the
        newest trade), or the page reaches stop_at_id (inclusive), if given.
        Raises PermanentError if a page does not move past from_id (infinite-loop guard).
        """
        while stop_at_id is None or from_id <= stop_at_id:
            params = {"symbol": symbol, "fromId": from_id, "limit": self.page_limit}
            rows = self.get_json(AGG_TRADES_PATH, params)
            fetched_at = datetime.now(UTC)

            if not isinstance(rows, list):
                raise PermanentError(
                    f"expected a JSON list from {AGG_TRADES_PATH}, got {type(rows).__name__}"
                )
            if not rows:
                logger.info("%s: empty page at fromId=%s -> caught up", symbol, from_id)
                return

            if rows[-1]["a"] < from_id:  # the infinite-loop guard, checked before anything is used
                raise PermanentError(
                    f"{symbol}: page does not move past fromId={from_id} "
                    f"(last id {rows[-1]['a']}); refusing to loop on it"
                )

            full_page = len(rows) == self.page_limit
            if stop_at_id is not None:
                rows = [r for r in rows if r["a"] <= stop_at_id]
                if not rows:
                    return

            first_id, last_id = rows[0]["a"], rows[-1]["a"]
            logger.info(
                "%s: page fromId=%s -> ids %s..%s (%s rows), used weight %s",
                symbol,
                from_id,
                first_id,
                last_id,
                len(rows),
                self.last_used_weight,
            )
            yield Page(endpoint=AGG_TRADES_PATH, params=params, rows=rows, fetched_at=fetched_at)

            if not full_page or (stop_at_id is not None and last_id >= stop_at_id):
                return

            from_id = last_id + 1  # the cursor: fromId is inclusive, so +1 or we re-fetch the last row
