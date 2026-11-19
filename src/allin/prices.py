"""Total-return price store: a frozen snapshot plus rows appended by the daily job.

`tr` is a total-return level (the dividend-adjusted close). Only ratios of `tr` between two dates are used, and a stored
row is never rewritten, so yesterday's published PnL cannot change when a data vendor restates its adjusted prices.
"""
from __future__ import annotations

import bisect
import csv
import datetime as dt
import gzip
import io
import pathlib
import time

SNAPSHOT = "prices_snapshot.csv.gz"
LIVE = "prices_live.csv"
CALENDAR = "SPY"                 # its trading days are the US sessions
# dollar series built from a foreign listing: local total return divided by the local-currency price of a dollar
SYNTHETIC = {"SKH-USD": ("000660.KS", "KRW=X"), "SAMSUNG-USD": ("005930.KS", "KRW=X")}


def us_listed(ticker: str) -> bool:
    """US-exchange symbols have no suffix; crypto pairs (-USD), currencies (=X) and foreign listings (.KS) do."""
    return not any(ch in ticker for ch in "-=.")


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


def _chain(stored_dates, stored_vals, bars, cutoff, ticker, tol=0.02):
    """New (date, level) rows after the last stored date, continuing the stored level with ratios of adjusted closes."""
    adj = {d: a for d, _, a in bars}
    if not stored_dates:                                          # a ticker seen for the first time: start from the vendor's level
        return [(d, a) for d, a in sorted(adj.items()) if d <= cutoff]
    last = stored_dates[-1]
    if last not in adj:
        raise RuntimeError(f"{ticker}: the download does not reach back to the last stored day {last}")
    # the vendor's recent moves must agree with what is stored, or the symbol now means something else
    for d, v in list(zip(stored_dates, stored_vals))[-6:-1]:
        if d in adj and abs((stored_vals[-1] / v) / (adj[last] / adj[d]) - 1) > tol:
            raise RuntimeError(f"{ticker}: stored history disagrees with the download between {d} and {last}")
    return [(d, stored_vals[-1] * a / adj[last]) for d, a in sorted(adj.items()) if last < d <= cutoff]


def update_live(data_dir: str | pathlib.Path, tickers: list[str], now_utc: dt.datetime | None = None, fetch=yahoo_history) -> list[tuple[dt.date, str, float]]:
    """Append final daily levels for `tickers` to the live file. Returns the rows added."""
    now_utc = now_utc or dt.datetime.now(dt.timezone.utc)
    d = pathlib.Path(data_dir); prices = load_prices(d); added: list[tuple[dt.date, str, float]] = []
    for t in sorted(set(tickers)):
        dates = prices._dates.get(t, []); vals = prices._values.get(t, [])
        start = (dates[-1] if dates else now_utc.date() - dt.timedelta(days=120)) - dt.timedelta(days=12)
        if t in SYNTHETIC:
            local, fx = SYNTHETIC[t]
            lb = fetch(local, start); fb = fetch(fx, start - dt.timedelta(days=5))
            fxd = sorted((x, c) for x, c, _ in fb)

            def rate(day, fxd=fxd):
                i = bisect.bisect_right([x for x, _ in fxd], day) - 1
                if i < 0:
                    raise RuntimeError(f"{fx}: no rate on or before {day}")
                return fxd[i][1]
            bars = [(x, c / rate(x), a / rate(x)) for x, c, a in lb]      # dollars: local level divided by local units per dollar
            cutoff = min(final_through(local, now_utc), final_through(fx, now_utc))
        else:
            bars = fetch(t, start); cutoff = final_through(t, now_utc)
        for day, level in _chain(dates, vals, bars, cutoff, t):
            added.append((day, t, level))
    if added:
        live = d / LIVE; new_file = not live.exists()
        with live.open("a", encoding="utf-8", newline="") as f:
            w = csv.writer(f, lineterminator="\n")
            if new_file:
                w.writerow(("date", "ticker", "tr"))
            for day, t, level in sorted(added):
                w.writerow((day.isoformat(), t, f"{level:.8g}"))
    return added
