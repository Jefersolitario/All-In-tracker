"""One-off check from GitHub's servers: the build branch's venue code against the live venues."""
import datetime as dt
from allin import venue
UTC = dt.timezone.utc
ts = lambda s: dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
now = dt.datetime.now(UTC)
got = venue.hyperliquid_candles("xyz:NVDA", ts("2026-11-21T10:00:00Z"), now)
print("hyperliquid xyz:NVDA candles since the stamp hour:", len(got), "| first:", got[0] if got else None, "| last:", got[-1] if got else None)
btc = venue.binance_candles("BTCUSDT", ts("2026-11-20T00:00:00Z"), ts("2026-11-21T00:00:00Z"))
print("binance BTCUSDT candles on 20 Nov:", len(btc), "| first:", btc[0], "| last:", btc[-1])
# the real lookup path on a call whose answer is on file: call 49, SpaceX and Tesla, entry day 9 Nov 2026
class C:  # the fields entries_for reads
    call_id = 49; legs = (("SPCX", 0.5), ("TSLA", 0.5))
symbols = {"SPCX": ("hyperliquid", "xyz:SPCX"), "TSLA": ("hyperliquid", "xyz:TSLA")}
rows = venue.entries_for(C, ts("2026-11-07T19:27:00Z"), dt.date(2026, 11, 9), symbols, venue.FETCHERS, "smoke")
for e in rows:
    print(e.ticker, e.venue, e.symbol, e.entry_utc, e.entry_price, e.close_utc, e.close_price, None if e.ratio is None else round(e.ratio, 5))
f = 1 + sum(0.5 * (e.ratio - 1) for e in rows)
print("factor for call 49:", round(f, 5), "| on file: 1.0553")
assert abs(f - 1.0553) < 3e-4 and len(got) > 0 and len(btc) in (24, 25)      # Binance counts the candle that opens at the end time
print("SMOKE OK")
