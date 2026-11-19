"""Total-return price store: a frozen snapshot plus rows appended by the daily job.

`tr` is a total-return level (the dividend-adjusted close). Only ratios of `tr` between two dates are used, and a stored
row is never rewritten, so yesterday's published PnL cannot change when a data vendor restates its adjusted prices.
"""
from __future__ import annotations

import bisect
import csv
import datetime as dt
import functools
import gzip
import io
import pathlib
import statistics
import time

SNAPSHOT = "prices_snapshot.csv.gz"
LIVE = "prices_live.csv"
CALENDAR = "SPY"                 # its trading days are the US sessions
# dollar series built from a foreign listing: local total return divided by the local-currency price of a dollar
SYNTHETIC = {"SKH-USD": ("000660.KS", "KRW=X"), "SAMSUNG-USD": ("005930.KS", "KRW=X")}


TRUST = "price_trust.csv"        # tickers whose vendor data the owner confirmed by hand, and from which stored day on
JUMP_BAND = (0.5, 2.0)          # a one-day move outside this is taken for a data error until someone confirms it
RATE_BAND = (0.9, 1.1)          # the same for a currency rate
ANCHORS = 7                     # stored bars that vote on each new level
AGREE = 1e-3                    # implied levels this close to each other count as the same
SPREAD_OK = 0.02                # with no majority, levels within this band are still used; beyond it the job stops


def us_listed(ticker: str) -> bool:
    """A US exchange symbol. Crypto pairs end in -USD, currencies in =X and foreign listings carry a dot suffix (.KS);
    a dash alone marks a US share class (BRK-B)."""
    return not (ticker.endswith("-USD") or ticker.endswith("=X") or "." in ticker)


def needs_own_day(ticker: str) -> bool:
    """True when the ticker must have a price of its own on every US session before that session is published:
    US symbols, and cryptocurrencies, which trade every day. The dollar series built from foreign listings do not,
    because their home market keeps other holidays; they are carried at their last close."""
    return ticker not in SYNTHETIC and not ticker.endswith("=X") and "." not in ticker


def _nth(year: int, month: int, weekday: int, n: int) -> dt.date:
    first = dt.date(year, month, 1)
    return first + dt.timedelta(days=(weekday - first.weekday()) % 7, weeks=n - 1)


def _easter(year: int) -> dt.date:
    a = year % 19; b, c = divmod(year, 100); d, e = divmod(b, 4); g = (b - (b + 8) // 25 + 1) // 3
    h = (19 * a + b - d - g + 15) % 30; i, k = divmod(c, 4); l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return dt.date(year, month, day + 1)


@functools.lru_cache(maxsize=None)
def market_holidays(year: int) -> frozenset[dt.date]:
    """Scheduled full-day closures of the New York Stock Exchange in one year, from its standing rules."""
    last_monday_of_may = dt.date(year, 5, 31) - dt.timedelta(days=dt.date(year, 5, 31).weekday())
    days = {_nth(year, 1, 0, 3), _nth(year, 2, 0, 3), _easter(year) - dt.timedelta(days=2), last_monday_of_may,
            _nth(year, 9, 0, 1), _nth(year, 11, 3, 4)}
    fixed = [dt.date(year, 1, 1), dt.date(year, 7, 4), dt.date(year, 12, 25)] + ([dt.date(year, 6, 19)] if year >= 2022 else [])
    for day in fixed:
        if day.weekday() == 6:
            days.add(day + dt.timedelta(days=1))                       # on a Sunday: closed the Monday after
        elif day.weekday() == 5:
            if day.month != 1:
                days.add(day - dt.timedelta(days=1))                   # on a Saturday: closed the Friday before, except for New Year's Day
        else:
            days.add(day)
    return frozenset(days)


def session_day(d: dt.date) -> bool:
    """False on weekends and scheduled exchange holidays, when a US symbol cannot have a bar of its own."""
    return d.weekday() < 5 and d not in market_holidays(d.year)


class Prices:
    def __init__(self, series: dict[str, dict[dt.date, float]]):
        self._dates = {t: sorted(d) for t, d in series.items()}
        self._values = {t: [series[t][k] for k in self._dates[t]] for t in series}

    def has(self, ticker: str) -> bool:
        return ticker in self._dates

    def sessions(self) -> list[dt.date]:
        return list(self._dates[CALENDAR])

    def last(self, ticker: str) -> dt.date | None:
        return self._dates[ticker][-1] if ticker in self._dates else None

    def on(self, ticker: str, d: dt.date) -> float | None:
        ds = self._dates.get(ticker, [])
        i = bisect.bisect_left(ds, d)
        return self._values[ticker][i] if i < len(ds) and ds[i] == d else None

    def asof(self, ticker: str, d: dt.date, max_back: int = 10) -> float | None:
        """Latest level on or before `d`, at most `max_back` calendar days old."""
        ds = self._dates.get(ticker, [])
        i = bisect.bisect_right(ds, d) - 1
        if i < 0 or (d - ds[i]).days > max_back:
            return None
        return self._values[ticker][i]

    def tickers(self) -> list[str]:
        return sorted(self._dates)


def _read(text: str, into: dict[str, dict[dt.date, float]], where: str) -> None:
    for r in csv.DictReader(io.StringIO(text)):
        d = dt.date.fromisoformat(r["date"]); v = float(r["tr"])
        if not v > 0:
            raise ValueError(f"{where}: non-positive level for {r['ticker']} on {d}")
        slot = into.setdefault(r["ticker"], {})
        if d in slot and abs(slot[d] / v - 1) > 1e-9:
            raise ValueError(f"{where}: {r['ticker']} {d} is stored twice with different values")
        slot[d] = v


def load_prices(data_dir: str | pathlib.Path) -> Prices:
    d = pathlib.Path(data_dir); series: dict[str, dict[dt.date, float]] = {}
    with gzip.open(d / SNAPSHOT, "rt", encoding="utf-8", newline="") as f:
        _read(f.read(), series, SNAPSHOT)
    live = d / LIVE
    if live.exists():
        _read(live.read_text(encoding="utf-8"), series, LIVE)
    return Prices(series)


# ---------- live update ----------
def final_through(ticker: str, now_utc: dt.datetime) -> dt.date:
    """Latest bar date whose close is final at `now_utc`."""
    if ticker in SYNTHETIC:
        return min(final_through(part, now_utc) for part in SYNTHETIC[ticker])
    if ticker.endswith(".KS"):
        lag = dt.timedelta(hours=7, minutes=30)           # Seoul closes 06:30 UTC
    elif ticker.endswith("=X") or ticker.endswith("-USD"):
        lag = dt.timedelta(hours=24, minutes=30)          # currency and crypto days end at midnight UTC or just before
    else:
        lag = dt.timedelta(hours=22, minutes=30)          # New York closes 20:00 UTC in summer, 21:00 UTC in winter
    return (now_utc - lag).date()


def _from_yfinance(symbol: str, start: dt.date) -> list[tuple[dt.date, float, float]]:
    import yfinance as yf
    h = yf.Ticker(symbol).history(start=start.isoformat(), interval="1d", auto_adjust=False)
    out = []
    for ts, row in h.iterrows():
        close, adj = float(row["Close"]), float(row["Adj Close"])
        if close == close and adj == adj and close > 0 and adj > 0:        # drops the empty rows Yahoo returns for a day in progress
            out.append((ts.date(), close, adj))
    return out


def _from_chart_api(symbol: str, start: dt.date) -> list[tuple[dt.date, float, float]]:
    import requests
    p1 = int(dt.datetime.combine(start, dt.time(), dt.timezone.utc).timestamp()); p2 = int(dt.datetime.now(dt.timezone.utc).timestamp()) + 86400
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}", params={"period1": p1, "period2": p2, "interval": "1d", "events": "div,splits"},
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]; off = res["meta"].get("gmtoffset", 0)
    closes = res["indicators"]["quote"][0]["close"]; adj = res["indicators"]["adjclose"][0]["adjclose"]
    return [(dt.datetime.fromtimestamp(t + off, dt.timezone.utc).date(), float(c), float(a)) for t, c, a in zip(res["timestamp"], closes, adj)
            if c is not None and a is not None and c > 0 and a > 0]


def yahoo_history(symbol: str, start: dt.date, attempts: int = 3, pause: float = 5.0) -> list[tuple[dt.date, float, float]]:
    """Daily bars from Yahoo Finance as (date in the exchange's time zone, close, dividend-adjusted close).
    Tries the yfinance package, then the plain chart endpoint, a few times each before giving up."""
    errors = []
    for attempt in range(attempts):
        for source in (_from_yfinance, _from_chart_api):
            try:
                out = source(symbol, start)
                if out:
                    return out
                errors.append(f"{source.__name__}: no rows")
            except Exception as e:  # noqa: BLE001 - try the other route, then again
                errors.append(f"{source.__name__}: {type(e).__name__}: {e}")
        if attempt + 1 < attempts:
            time.sleep(pause * (attempt + 1))
    raise RuntimeError(f"no price history for {symbol}: " + "; ".join(errors[-4:]))


def load_trust(data_dir: str | pathlib.Path) -> dict[str, dt.date]:
    """For each ticker the owner confirmed by hand: the first day stored in that run. Stored bars before it no longer vote."""
    path = pathlib.Path(data_dir) / TRUST; out: dict[str, dt.date] = {}
    if path.exists():
        with path.open(newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                day = dt.date.fromisoformat(r["from_date"])
                out[r["ticker"]] = max(day, out.get(r["ticker"], day))
    return out


def _consensus(implied: list[tuple[dt.date, float]], ticker: str, trust: bool) -> float:
    """The level for a new day, given what each recent stored bar implies for it (oldest first).

    Normally all of them imply the same level. When the vendor has since changed some of those days (a dividend booked
    late, a corrected close), the bars split into camps. A camp that holds more than half wins: that is how a late
    dividend is caught up, and why it is never undone afterwards. On an exact half the camp of the newest bar stays;
    catching up takes a majority. With no camp at half, levels close together are averaged by their median; far apart,
    the symbol may mean something else now, and only the owner can say.
    """
    levels = [x for _, x in implied]
    camp = lambda c: [x for x in levels if abs(x / c - 1) <= AGREE]    # noqa: E731
    newest = best = camp(levels[-1])
    for c in levels:                                                  # oldest first; a tie with the newest bar's camp leaves that camp in place
        if len(camp(c)) > len(best):
            best = camp(c)
    if len(best) * 2 > len(levels):
        return statistics.median(best)
    if len(newest) * 2 == len(levels):
        return statistics.median(newest)
    if max(levels) / min(levels) - 1 <= SPREAD_OK:
        return statistics.median(levels)
    if trust:
        return statistics.median(best)
    raise RuntimeError(f"{ticker}: the last stored bars and the download disagree: they imply {min(levels):.6g} to {max(levels):.6g} "
                       f"for the first bar after {implied[-1][0]} and no majority agrees. Nothing was stored. If the vendor is right, run again with --allow-jump {ticker}.")


def _chain(stored_dates, stored_vals, bars, cutoff, ticker, trust=False, anchors_from=None, skip_through=None):
    """New (date, level) rows after the last stored date, continuing the stored levels with ratios of adjusted closes.
    Each new level is what most of the last few stored bars imply for that day; no stored row is ever touched.
    Days up to `skip_through` are left out: they were published without this bar."""
    adj = {d: a for d, _, a in bars}
    last = stored_dates[-1] if stored_dates else None
    new_days = [d for d in sorted(adj) if d <= cutoff and (last is None or d > last) and (skip_through is None or d > skip_through)]
    if last is None or not new_days:                                   # a ticker seen for the first time starts at the vendor's level
        return [(d, adj[d]) for d in new_days]
    stored = list(zip(stored_dates, stored_vals))
    voters = [(d, v) for d, v in stored if anchors_from is None or d >= anchors_from] or stored[-1:]
    anchors = [(d, v) for d, v in voters[-ANCHORS:] if d in adj]
    if not anchors:
        if trust:
            return [(d, adj[d]) for d in new_days]                     # the owner's call: the series starts again at the vendor's level
        raise RuntimeError(f"{ticker}: none of the last {ANCHORS} stored days is in the download (the last is {last}), so the series cannot be continued. "
                           f"If its call is over or has not started, run again with --allow-jump {ticker} and the series starts again at the vendor's level. "
                           "If the ticker is held, close its call with a forced_exit instead.")
    return [(d, _consensus([(k, v * adj[d] / adj[k]) for k, v in anchors], ticker, trust)) for d in new_days]


def update_live(data_dir: str | pathlib.Path, tickers: list[str], now_utc: dt.datetime | None = None, fetch=yahoo_history,
                published_through: dt.date | None = None, allow_jump: frozenset[str] | set[str] = frozenset(),
                optional: frozenset[str] | set[str] = frozenset()) -> list[tuple[dt.date, str, float]]:
    """Append final daily levels for `tickers` to the live file. Returns the rows added. Nothing is written if anything fails.

    - A bar dated on or before `published_through` is never stored: that day has been published without it, and a price
      that turns up late (a foreign bar the vendor had not posted yet) would change a published day. Later bars chain
      over the gap, so the level stays right and the missed move is recognised one bar later.
    - A bar that is less than half or more than twice the vendor's own bar before it is refused: an unadjusted split or
      a bad print would otherwise be stored for good.
    - For the dollar series built from a foreign listing, a currency rate more than a tenth away from the rates of the
      two bars before it is taken for a bad print: that bar is left out with a warning, and the series is carried at
      its last close, as over a holiday. Nothing stops, and the next good bar brings the level up to date.
    - A US symbol's bar dated on a weekend or an exchange holiday is dropped: it would turn a closed day into a session.
    - A ticker in `allow_jump` is one whose vendor data the owner has confirmed: the checks above and the vote of the
      stored bars are waived for it in this run, and the decision is written down, so that later runs let only the bars
      stored from this run on vote for it.
    - A ticker in `optional` (its call is about to be closed by a forced exit, so it may have stopped trading) is
      skipped when it cannot be downloaded or continued.
    """
    now_utc = now_utc or dt.datetime.now(dt.timezone.utc)
    d = pathlib.Path(data_dir); prices = load_prices(d); trusted = load_trust(d); added: list[tuple[dt.date, str, float]] = []
    for t in sorted(set(tickers)):
        dates = prices._dates.get(t, []); vals = prices._values.get(t, [])
        start = (dates[-1] if dates else now_utc.date() - dt.timedelta(days=120)) - dt.timedelta(days=12)
        rates: dict[dt.date, float] = {}
        try:
            if t in SYNTHETIC:
                local, fx = SYNTHETIC[t]
                lb = fetch(local, start); fb = fetch(fx, start - dt.timedelta(days=5))
                fxd = sorted((x, c) for x, c, _ in fb); fx_days = [x for x, _ in fxd]

                def rate(day):
                    i = bisect.bisect_right(fx_days, day) - 1
                    if i < 0:
                        raise RuntimeError(f"{fx}: no rate on or before {day}")
                    return fxd[i][1]
                rates = {x: rate(x) for x, _, _ in lb}
                bars = [(x, c / rates[x], a / rates[x]) for x, c, a in lb]    # dollars: local level divided by local units per dollar
            else:
                bars = fetch(t, start)
            if us_listed(t):
                closed = sorted(x for x, _, _ in bars if not session_day(x) and (not dates or x > dates[-1]))
                if closed:
                    print(f"{t}: the vendor has a bar on {', '.join(map(str, closed))}, when the exchange is closed; dropped")
                bars = [b for b in bars if session_day(b[0])]
            rows = _chain(dates, vals, bars, final_through(t, now_utc), t, trust=t in allow_jump, anchors_from=trusted.get(t), skip_through=published_through)
        except Exception as e:  # noqa: BLE001
            if t in optional:
                print(f"{t}: not updated ({e}); its call has a forced exit, so the last stored price is carried")
                continue
            raise
        adj = {x: a for x, _, a in bars}; order = sorted(adj); before = {order[i]: order[i - 1] for i in range(1, len(order))}
        for day, level in rows:
            prev = before.get(day)
            if prev is not None and t not in allow_jump:
                if rates:
                    far = [not RATE_BAND[0] <= rates[day] / rates[p] <= RATE_BAND[1] for p in (prev, before.get(prev)) if p is not None]
                    if all(far):                                       # off against the two rates before it; off against the last one only, that one was the bad print
                        print(f"::warning::{SYNTHETIC[t][1]}: the rate used for {day} is {rates[day] / rates[prev]:.2f} times the one before it; "
                              f"the bar is left out and {t} is carried at its last close. If the move is real, run again with --allow-jump {t}.")
                        continue
                    if far[0]:
                        prev = before[prev]                            # the bar before was built on the bad print: compare with the one before that
                if not JUMP_BAND[0] <= adj[day] / adj[prev] <= JUMP_BAND[1]:
                    raise RuntimeError(f"{t}: the bar of {day} is {adj[day] / adj[prev]:.2f} times the one before it. "
                                       f"Nothing was stored. If the move is real, run again with --allow-jump {t}.")
            added.append((day, t, level))
    if added:
        confirmed = sorted((t, min(day for day, x, _ in added if x == t)) for t in set(allow_jump) if any(x == t for _, x, _ in added))
        if confirmed:                                                  # written first: a note without rows is harmless, rows without the note stop the next run
            note = d / TRUST; new_note = not note.exists()
            with note.open("a", encoding="utf-8", newline="") as f:
                w = csv.writer(f, lineterminator="\n")
                if new_note:
                    w.writerow(("ticker", "from_date", "confirmed_utc"))
                for t, day in confirmed:
                    w.writerow((t, day.isoformat(), now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")))
        live = d / LIVE; new_file = not live.exists()
        with live.open("a", encoding="utf-8", newline="") as f:
            w = csv.writer(f, lineterminator="\n")
            if new_file:
                w.writerow(("date", "ticker", "tr"))
            for day, t, level in sorted(added):
                w.writerow((day.isoformat(), t, f"{level:.8g}"))
    return added
