## Business question
For the same instrument, do prices and volumes agree across venues and across a
venue's own data products — and when they don't, is it a market event or a data defect?

## Sources
| Source | Data | Access | Pagination | Scope now |
|---|---|---|---|---|
| Binance REST (data-api.binance.vision) | aggTrades (klines, exchangeInfo: stretch) | public, weight-limited per IP | fromId cursor | BTCUSDT |
| Binance bulk (data.binance.vision) | daily aggTrades, 1m klines | zip + .CHECKSUM | by date | 3 symbols × 4 days |
| Kraken REST (W9) | trades, OHLC | public | since/last cursor; OHLC ≤ 720 per call | — |

## Entities and grain
- Trade: one row per (symbol, agg_trade_id) — natural key
- Candle: one row per (symbol, minute)
- Instrument: SCD2 — tick size, lot size, min notional, status. The API returns only
  the current state, so history exists only if we snapshot it.

## Domain criteria (roadmap 9.1)
1. API with pagination and rate limits — yes: fromId cursor, weight limits, 429/418.
2. Bulk volume for Spark — measured 7.9M aggTrades for 3 symbols × 4 days; scales by months.
3. Changing entity — instrument metadata; caveat: history must be collected from now on.
4. Business question — above.

## Known quirks (→ week-7 tests)
- Bulk timestamps switch from ms to µs on 2025-01-01
- Some bulk files have a header row, some don't
- 429 = rate-limited; 418 = banned for continuing after 429s
- ETHBTC is thin: minutes with no trades are normal, not failures
- ETHBTC quote_volume has rounding residue (~4e-6) → needs a relative tolerance
- 30 BTCUSDT minutes have trades but no candle — cause: ______