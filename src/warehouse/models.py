from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class AggTrade:
    """One Binance aggregate trade (keys from /api/v3/aggTrades in comments).

    frozen=True: once created it cannot be changed, so no step can quietly alter a trade another step already checked.
    """

    symbol: str
    agg_trade_id: int  # a
    price: Decimal  # p  (Decimal, never float: money must be exact)
    qty: Decimal  # q
    first_trade_id: int  # f
    last_trade_id: int  # l
    ts_us: int  # T  (always microseconds after normalising)
    is_buyer_maker: bool  # m
    is_best_match: bool  # M

    def __post_init__(self) -> None:
        for name in ("price", "qty"):
            if not isinstance(getattr(self, name), Decimal):
                raise TypeError(f"{name} must be Decimal, got {type(getattr(self, name)).__name__}")
