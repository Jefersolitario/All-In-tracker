"""Buying a call on a venue that trades around the clock, instead of waiting for the close.

The second paper strategy holds the same book as the first. The one difference: a new call is bought on a 24/7 venue
at the first hourly open after the call was recorded, not at the close of the next US session. What that is worth is
measured on the venue itself: its price at the moment the rule buys, divided by its price at the early buy. The book
gets that ratio on the entry day; from then on both strategies hold the same thing and are valued the same way.

Every buy is written once to data/venue_entries.csv and never changed: the times follow from the stamp and the
calendar, and the prices are the venue's public hourly candles, so anyone can check a line afterwards.
"""
from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import pathlib
from zoneinfo import ZoneInfo

from . import ledger, prices
from .ledger import Call

ENTRIES = "venue_entries.csv"       # one line per leg of a call that was looked up on a venue
SYMBOLS = "venue_symbols.csv"       # ledger ticker -> venue and its symbol; the owner's to edit
COLUMNS = ("call_id", "ticker", "venue", "symbol", "entry_utc", "entry_price", "close_utc", "close_price", "basis")
NY = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
HOUR = dt.timedelta(hours=1)
MAX_WAIT = dt.timedelta(hours=6)    # a venue with no candle this close to the moment has no usable price there
SETTLE = dt.timedelta(minutes=5)    # how long after a close its hourly candle is taken as final
STAMP = "%Y-%m-%dT%H:%M:%SZ"
HYPERLIQUID = "https://api.hyperliquid.xyz/info"
BINANCE = "https://data-api.binance.vision/api/v3/klines"


@dataclasses.dataclass(frozen=True)
class Entry:
    call_id: int
    ticker: str
    venue: str                          # "none" when the leg was not bought early
    symbol: str
    entry_utc: dt.datetime | None       # open time of the hourly candle the leg was bought at
    entry_price: float | None
    close_utc: dt.datetime | None       # end of the last hourly candle before the rule's own buy
    close_price: float | None
    basis: str

    @property
    def ratio(self) -> float | None:
        """The venue's price when the rule buys, over its price at the early buy. None: this leg waits for the close."""
        if self.entry_price is None or self.close_price is None:
            return None
        return self.close_price / self.entry_price


def _ts(text: str) -> dt.datetime | None:
    return dt.datetime.strptime(text, STAMP).replace(tzinfo=UTC) if text else None


def load_symbols(data_dir: str | pathlib.Path) -> dict[str, tuple[str, str]]:
    path = pathlib.Path(data_dir) / SYMBOLS
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as f:
        return {r["ticker"]: (r["venue"], r["symbol"]) for r in csv.DictReader(f)}


def load_entries(data_dir: str | pathlib.Path) -> list[Entry]:
    path = pathlib.Path(data_dir) / ENTRIES
    if not path.exists():
        return []
    out = []
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if tuple(reader.fieldnames or ()) != COLUMNS:
            raise ValueError(f"{ENTRIES}: the header must be {','.join(COLUMNS)}")
        for n, r in enumerate(reader, start=2):
            try:
                out.append(Entry(int(r["call_id"]), r["ticker"], r["venue"], r["symbol"], _ts(r["entry_utc"]), float(r["entry_price"]) if r["entry_price"] else None,
                                 _ts(r["close_utc"]), float(r["close_price"]) if r["close_price"] else None, r["basis"]))
            except (TypeError, ValueError) as e:
                raise ValueError(f"{ENTRIES} line {n}: {e}") from None
    return out


def buy_hour(t: dt.datetime) -> dt.datetime:
    """The first whole hour at or after `t`: hourly candles open on the hour."""
    floor = t.replace(minute=0, second=0, microsecond=0)
    return floor if floor == t else floor + HOUR


def close_time(ticker: str, day: dt.date) -> dt.datetime:
    """The moment the rule's own price for `day` is struck: the New York close for US symbols, the end of the UTC day
    for cryptocurrencies, and the Seoul close for the dollar series built from Seoul listings.
    A half day in New York still counts as 16:00; the venue is read three hours after that day's official close."""
    if ticker in prices.SYNTHETIC:
        return dt.datetime.combine(day, dt.time(6, 30), UTC)          # 15:30 in Seoul; Korea keeps no summer time
    if ticker.endswith("-USD"):
        return dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0), UTC)
    return dt.datetime.combine(day, dt.time(16), NY).astimezone(UTC)


def _ask(send, attempts: int = 3, pause: float = 4.0):
    """One request, tried a few times: a venue that hiccups once should not cost the night's run."""
    import time
    for attempt in range(attempts):
        try:
            r = send(); r.raise_for_status()
            return r.json()
        except Exception:  # noqa: BLE001
            if attempt + 1 == attempts:
                raise
            time.sleep(pause * (attempt + 1))


def hyperliquid_candles(symbol: str, start: dt.datetime, end: dt.datetime) -> list[tuple[dt.datetime, float, float]]:
    """Hourly candles as (open time, open, close). An empty list means the venue has none there; a failed request raises."""
    import requests
    body = {"type": "candleSnapshot", "req": {"coin": symbol, "interval": "1h", "startTime": int(start.timestamp() * 1000), "endTime": int(end.timestamp() * 1000)}}
    got = _ask(lambda: requests.post(HYPERLIQUID, json=body, headers={"User-Agent": "Mozilla/5.0"}, timeout=30))
    return sorted((dt.datetime.fromtimestamp(c["t"] / 1000, UTC), float(c["o"]), float(c["c"])) for c in (got or []))


def binance_candles(symbol: str, start: dt.datetime, end: dt.datetime) -> list[tuple[dt.datetime, float, float]]:
    import requests
    query = {"symbol": symbol, "interval": "1h", "startTime": int(start.timestamp() * 1000), "endTime": int(end.timestamp() * 1000), "limit": 1000}
    got = _ask(lambda: requests.get(BINANCE, params=query, headers={"User-Agent": "Mozilla/5.0"}, timeout=30))
    return sorted((dt.datetime.fromtimestamp(k[0] / 1000, UTC), float(k[1]), float(k[4])) for k in got)


FETCHERS = {"hyperliquid": hyperliquid_candles, "binance": binance_candles}


def entries_for(call: Call, when: dt.datetime, entry_day: dt.date, symbols: dict[str, tuple[str, str]], fetchers: dict, basis: str) -> list[Entry]:
    """One line per leg: bought at the first hourly open at or after `when`, compared with the venue's last hourly close
    before the rule buys on `entry_day`. A leg with no market, or no candle near either moment, waits for the close."""
    out = []
    first = buy_hour(when)
    for ticker, _ in call.legs:
        if ticker not in symbols:
            out.append(Entry(call.call_id, ticker, "none", "", None, None, None, None, "no venue market on file for this ticker")); continue
        venue, symbol = symbols[ticker]
        if venue not in fetchers:
            raise ValueError(f"{SYMBOLS}: unknown venue {venue!r} for {ticker}")
        struck = close_time(ticker, entry_day)
        try:
            candles = fetchers[venue](symbol, first, struck + HOUR)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"venue: cannot download {symbol} from {venue} for call {call.call_id} ({type(e).__name__}: {e}). Nothing was written; the next run tries again. "
                               f"To give up on buying this call early, add a line for each of its legs to {ENTRIES} with venue 'none' and no prices.") from e
        buy = next(((t, o) for t, o, _ in candles if first <= t < min(first + MAX_WAIT, struck)), None)
        end = next(((t + HOUR, c) for t, _, c in reversed(candles) if struck - MAX_WAIT <= t + HOUR <= struck), None)
        if buy is None or end is None or end[0] <= buy[0]:
            why = "no venue candle within six hours of the buy" if buy is None else "no venue candle within six hours before the rule's close"
            out.append(Entry(call.call_id, ticker, "none", symbol, None, None, None, None, f"{venue}: {why}")); continue
        out.append(Entry(call.call_id, ticker, venue, symbol, buy[0], buy[1], end[0], end[1], basis))
    return out


def _line(e: Entry) -> tuple:
    px = lambda x: "" if x is None else f"{x:.10g}"                    # noqa: E731
    ts = lambda t: "" if t is None else t.strftime(STAMP)              # noqa: E731
    return (e.call_id, e.ticker, e.venue, e.symbol, ts(e.entry_utc), px(e.entry_price), ts(e.close_utc), px(e.close_price), e.basis)


def append_entries(data_dir: str | pathlib.Path, rows: list[Entry]) -> None:
    path = pathlib.Path(data_dir) / ENTRIES
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        if new:
            w.writerow(COLUMNS)
        w.writerows(_line(e) for e in rows)


def update(data_dir: str | pathlib.Path, calls: list[Call], sessions: list[dt.date], recovered_through: int, now_utc: dt.datetime, fetchers: dict | None = None) -> list[Entry]:
    """Look up, once, every stamped call whose entry session is stored and over. Returns the lines written."""
    done = {e.call_id for e in load_entries(data_dir)}
    symbols = load_symbols(data_dir); added: list[Entry] = []
    for c in calls:
        if c.status != "active" or c.call_id <= recovered_through or c.recorded_utc is None or c.call_id in done:
            continue
        day = ledger.entry_date(c, sessions)
        if day is None or now_utc < max(close_time(t, day) for t, _ in c.legs) + SETTLE:
            continue
        rows = entries_for(c, c.recorded_utc, day, symbols, fetchers or FETCHERS, "paper: first hourly open after the call was recorded")
        append_entries(data_dir, rows); added += rows
    return added


def check(entries: list[Entry], calls: list[Call], sessions: list[dt.date], recovered_through: int) -> None:
    """Refuse a file that cannot be the job's own work: a leg listed twice, a call or leg that does not exist, prices
    out of order, or, for a call traded on paper, a buy that is not the first hours after its stamp."""
    by_id = {c.call_id: c for c in calls}; seen = set()
    for e in entries:
        where = f"{ENTRIES}, call {e.call_id} {e.ticker}"
        if (e.call_id, e.ticker) in seen:
            raise ValueError(f"{where}: listed twice")
        seen.add((e.call_id, e.ticker))
        c = by_id.get(e.call_id)
        if c is None or e.ticker not in {t for t, _ in c.legs}:
            raise ValueError(f"{where}: no such call or leg in the ledger")
        given = [x is not None for x in (e.entry_utc, e.entry_price, e.close_utc, e.close_price)]
        if e.venue == "none" or not any(given):
            if any(given) or e.venue != "none":
                raise ValueError(f"{where}: a leg that waits for the close has venue 'none' and carries no times and no prices")
            continue
        if not all(given) or not (e.entry_price > 0 and e.close_price > 0) or e.close_utc <= e.entry_utc:
            raise ValueError(f"{where}: needs two positive prices and a buy before the close")
        if e.call_id <= recovered_through:
            continue                                                   # backtest lines: bought one hour after the episode, checked when they were made
        day = ledger.entry_date(c, sessions) if c.recorded_utc is not None else None
        if day is None:
            raise ValueError(f"{where}: the call has no stamp or its entry session is not stored yet, so it cannot have been bought")
        first, struck = buy_hour(c.recorded_utc), close_time(e.ticker, day)
        if not (first <= e.entry_utc < first + MAX_WAIT) or e.entry_utc != buy_hour(e.entry_utc):
            raise ValueError(f"{where}: bought at {e.entry_utc:{STAMP}}, which is not an hourly open in the six hours from {first:{STAMP}}, the first after its stamp")
        if not (struck - MAX_WAIT <= e.close_utc <= struck):
            raise ValueError(f"{where}: compared at {e.close_utc:{STAMP}}, which is not within six hours before the rule's close at {struck:{STAMP}}")


def factors(entries: list[Entry], calls: list[Call]) -> dict[int, float]:
    """For each call bought early: what a dollar put in on the venue is worth when the rule buys. Legs that waited count as 1."""
    by_call: dict[int, dict[str, float]] = {}
    for e in entries:
        if e.ratio is not None:
            by_call.setdefault(e.call_id, {})[e.ticker] = e.ratio
    out = {}
    for c in calls:
        if c.call_id in by_call:
            out[c.call_id] = max(1.0 + sum(w * (by_call[c.call_id][t] - 1.0) for t, w in c.legs if t in by_call[c.call_id]), 0.0)
    return out


def not_looked_up(entries: list[Entry], calls: list[Call], sessions: list[dt.date], recovered_through: int, through: dt.date) -> list[int]:
    """Stamped calls that have entered by `through` and have no line yet: their entry day cannot be published without one."""
    done = {e.call_id for e in entries}
    return [c.call_id for c in calls if c.status == "active" and c.call_id > recovered_through and c.recorded_utc is not None and c.call_id not in done
            and (ledger.entry_date(c, sessions) or dt.date.max) <= through]
