"""Pure data-cleaning functions: no network, no database, easy to test (T034)."""

# Epoch timestamps in 2020-2100 are ~1.6e12 in milliseconds and ~1.6e15 in
# microseconds. Anything below 1e14 must therefore be milliseconds.
_US_THRESHOLD = 10**14


def normalise_ts_to_us(ts: int) -> int:
    """Return the timestamp in microseconds, whether it arrived in ms or µs.

    Binance spot files switched from ms to µs on 2025-01-01. Deciding by
    magnitude (not by date) means the code is right for either unit.
    """
    return ts * 1000 if ts < _US_THRESHOLD else ts


def parse_bool(value: str | None) -> bool:
    """Binance CSVs write booleans as 'True'/'False' (case-insensitive)."""
    if not isinstance(value, str):
        raise ValueError(f"not a boolean: {value!r}")
    v = value.strip().lower()
    if v in ("true", "1"):
        return True
    if v in ("false", "0"):
        return False
    raise ValueError(f"not a boolean: {value!r}")
