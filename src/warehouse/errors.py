"""Classified failures.

A failure is classified where its meaning is known (client.py, from the HTTP
status). Retry code then checks the *type* and never parses error messages.

    WarehouseError
    ├── TransientError   5xx, timeout, connection drop -> back off and retry
    ├── RateLimited      HTTP 429 -> sleep Retry-After, then retry
    ├── Banned           HTTP 418 -> stop the whole run, never retry
    └── PermanentError   other 4xx, bad data -> fail now, retrying won't help
"""

class WarehouseError(Exception):
    """Base class:`except WarehouseError` catches every failure we classify."""

class TransientError(WarehouseError):
    """Temporary problem; the same request may succeed later."""

class RateLimited(WarehouseError):
    """Binance returned 429: we exceeded the request-weight limit."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"rate limited (429); retry after {retry_after}s")
        self.retry_after=retry_after

class Banned(WarehouseError):
    """Binance returned 418: IP auto-banned for continuing after 429s."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"IP banned (418); ban lifts in {retry_after}s")
        self.retry_after = retry_after

class PermanentError(WarehouseError):
    """The request itself is wrong; retrying will not fix it."""