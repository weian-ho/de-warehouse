Q12
Implemented the SCD2 read pattern: as-of join from 7.9M trades to a versioned instrument dimension using half-open intervals, with a uniqueness check proving every fact matches exactly one version — including trades on a version boundary. Enforced non-overlap structurally with a GiST exclusion constraint rather than relying on the check alone.


Q14
Reconciled 7.9M aggregated trades against 1-minute candles across 3 symbols and 4 days. ETHUSDT and BTCUSDT tie exactly; ETHBTC shows ~4e-6 rounding residue requiring a relative tolerance. Found 30 BTCUSDT minutes with trades but no candle, traced to [cause].
