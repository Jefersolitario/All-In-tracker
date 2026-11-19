"""One shared portfolio: every active call holds an equal share, reset whenever a call starts or ends."""
from __future__ import annotations

import bisect
import dataclasses
import datetime as dt
import math

from .ledger import Call, entry_date, exit_date
from .prices import Prices, us_listed


@dataclasses.dataclass(frozen=True)
class Position:
    call: Call
    rule_entry: dt.date | None  # what the calendar rule gives; None while that session has not happened yet
    entry: dt.date | None       # when this book buys it: later than rule_entry for a book that starts afterwards
    exit: dt.date | None        # None while the exit day is beyond the stored sessions


@dataclasses.dataclass
class Result:
    dates: list[dt.date]
    equity: list[float]
    n_active: list[int]
    n_trades: list[int]
    holdings: dict[int, float]              # call id -> dollars at the last close
    positions: dict[int, Position]
    call_pnl: list[dict[int, float]] = dataclasses.field(default_factory=list)   # per date: call id -> dollars made that day


def plan(calls: list[Call], sessions: list[dt.date], not_before: dt.date | None = None) -> list[Position]:
    """Entry and exit day of every active call.

    With `not_before`, the book starts on that day: a call whose rule entry is earlier is bought at the close of the
    first session on or after it and still leaves on its original exit day; a call that had already left is skipped.
    """
    out = []
    for c in calls:
        if c.status != "active":
            continue
        rule = entry_date(c, sessions)
        if rule is None:
            out.append(Position(c, None, None, None))
            continue
        leave = exit_date(rule, sessions, c.forced_exit)
        entry = rule
        if not_before is not None and rule < not_before:
            i = bisect.bisect_left(sessions, not_before)
            entry = sessions[i] if i < len(sessions) else None
            if leave is not None and leave <= (entry or not_before):
                continue
        out.append(Position(c, rule, entry, leave))
    return out


def call_value(call: Call, entry: dt.date, day: dt.date, prices: Prices) -> float | None:
    """Value on `day` of one dollar put into the call at the entry close; legs are bought once and held."""
    v = 1.0
    for ticker, w in call.legs:
        a = prices.asof(ticker, entry, 4); b = prices.asof(ticker, day)
        if a is None or b is None:
            return None
        v += w * (b / a - 1)
    return v


def complete_through(positions: list[Position], prices: Prices) -> dt.date:
    """Last session on which every open position can be valued with that day's own US prices."""
    sessions = prices.sessions()
    for d in reversed(sessions):
        ok = True
        for p in positions:
            if p.entry is None or p.entry > d or (p.exit is not None and p.exit < d):
                continue
            for ticker, _ in p.call.legs:
                if (us_listed(ticker) and prices.on(ticker, d) is None) or prices.asof(ticker, d) is None:
                    ok = False
        if ok:
            return d
    raise RuntimeError("no session on which all positions can be valued")


def simulate(positions: list[Position], prices: Prices, start_cash: float = 10_000.0, end: dt.date | None = None) -> Result:
    end = end or complete_through(positions, prices)
    pos = {p.call.call_id: p for p in positions if p.entry is not None and p.entry <= end}
    if not pos:
        raise RuntimeError("no call has entered yet")
    start = min(p.entry for p in pos.values())
    days = [d for d in prices.sessions() if start <= d <= end]
    hold: dict[int, float] = {}; last_value: dict[int, float] = {}; cash = start_cash
    out = Result([], [], [], [], {}, pos)
    for i, d in enumerate(days):
        made: dict[int, float] = {}
        if i > 0:
            for k in hold:
                v = call_value(pos[k].call, pos[k].entry, d, prices)
                if v is None:
                    raise RuntimeError(f"call {k} cannot be valued on {d}")
                made[k] = hold[k] * (v / last_value[k] - 1)
                hold[k] += made[k]; last_value[k] = v
        equity = cash + sum(hold.values())
        ending = [k for k in hold if pos[k].exit == d]
        starting = [k for k, p in pos.items() if p.entry == d]
        if ending or starting:
            keep = [k for k in hold if k not in ending] + starting
            hold = {k: equity / len(keep) for k in keep}; cash = 0.0 if keep else equity
            for k in starting:
                if call_value(pos[k].call, pos[k].entry, d, prices) is None:
                    raise RuntimeError(f"call {k} has no entry price on {d}")
                last_value[k] = 1.0
        out.dates.append(d); out.equity.append(equity); out.n_active.append(len(hold)); out.n_trades.append(len(ending) + len(starting))
        out.call_pnl.append(made)
    out.holdings = dict(hold)
    return out


def stats(equity: list[float], days_per_year: int = 252, rf_daily: list[float] | None = None) -> dict:
    """Dollar statistics in the dashboard's convention, plus return-based ones for comparison with the recovered table."""
    pnl = [equity[i] - equity[i - 1] for i in range(1, len(equity))]
    ret = [equity[i] / equity[i - 1] - 1 for i in range(1, len(equity))]
    n = len(pnl)

    def sharpe(x):
        m = sum(x) / n; sd = math.sqrt(sum((v - m) ** 2 for v in x) / (n - 1))
        return m / sd * math.sqrt(days_per_year) if sd > 0 else None
    mean = sum(pnl) / n; down = math.sqrt(sum(min(v, 0.0) ** 2 for v in pnl) / n)
    peak = equity[0]; dd_usd = 0.0; dd_pct = 0.0
    for v in equity:
        peak = max(peak, v); dd_usd = max(dd_usd, peak - v); dd_pct = min(dd_pct, v / peak - 1)
    out = {"pnl": equity[-1] - equity[0], "return_pct": (equity[-1] / equity[0] - 1) * 100, "sharpe": sharpe(pnl),
           "sortino": mean / down * math.sqrt(days_per_year) if down > 0 else None, "max_dd": dd_usd, "max_dd_pct": dd_pct * 100,
           "sharpe_per_dollar": sharpe(ret)}
    if rf_daily is not None:
        out["sharpe_vs_bills"] = sharpe([r - f for r, f in zip(ret, rf_daily)])
    return out
