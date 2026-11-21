"""Tests for the second paper strategy: a call bought on a 24/7 venue before the close the rule buys at."""
import datetime as dt
import json
import shutil

import polars as pl
import pytest

from allin import cli, engine, ledger, prices, venue
from test_allin import MARK, ROOT, UTC, make_call, small_store, stand_in_vendor

BASIS = "paper: first hourly open after the call was recorded"


def ts(text):
    return dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def candles(*rows):
    """A venue for tests: hourly candles given as (open time, open, close); asked for anything else, it has nothing."""
    book = {}
    for symbol, when, o, c in rows:
        book.setdefault(symbol, []).append((ts(when), o, c))

    def fetch(symbol, start, end):
        return sorted(x for x in book.get(symbol, []) if start <= x[0] < end)
    return fetch


def test_the_buy_is_the_next_hourly_open_and_the_comparison_is_the_rules_own_moment():
    assert venue.buy_hour(ts("2026-11-21T09:52:03Z")) == ts("2026-11-21T10:00:00Z")
    assert venue.buy_hour(ts("2026-11-21T10:00:00Z")) == ts("2026-11-21T10:00:00Z")
    assert venue.close_time("NVDA", dt.date(2026, 11, 23)) == ts("2026-11-23T21:00:00Z")           # New York winter time
    assert venue.close_time("NVDA", dt.date(2026, 6, 8)) == ts("2026-06-08T20:00:00Z")             # and summer time
    assert venue.close_time("BTC-USD", dt.date(2026, 11, 23)) == ts("2026-11-24T00:00:00Z")        # crypto days end at midnight UTC
    assert venue.close_time("SKH-USD", dt.date(2026, 5, 11)) == ts("2026-05-11T06:30:00Z")         # the Seoul close, long before New York's


def test_each_leg_is_bought_once_or_waits_for_the_close():
    call = make_call(call_id=60, legs="NVDA:0.25;SKH-USD:0.25;FIG:0.25;MU:0.25", recorded="2026-11-21T09:52:03Z")
    symbols = {"NVDA": ("hyperliquid", "xyz:NVDA"), "SKH-USD": ("hyperliquid", "xyz:SKHX"), "MU": ("hyperliquid", "xyz:MU")}
    fetch = candles(("xyz:NVDA", "2026-11-21T09:00:00Z", 1.0, 1.0),                                # before the stamp: never used
                    ("xyz:NVDA", "2026-11-21T12:00:00Z", 128.5, 128.9), ("xyz:NVDA", "2026-11-23T20:00:00Z", 130.0, 131.07),
                    ("xyz:NVDA", "2026-11-23T21:00:00Z", 999.0, 999.0),                             # after the rule's close: never used
                    ("xyz:SKHX", "2026-11-21T10:00:00Z", 800.0, 801.0), ("xyz:SKHX", "2026-11-23T05:00:00Z", 815.0, 816.0),
                    ("xyz:SKHX", "2026-11-23T06:00:00Z", 999.0, 999.0),                             # ends after the Seoul close
                    ("xyz:MU", "2026-11-21T17:00:00Z", 600.0, 600.0), ("xyz:MU", "2026-11-23T20:00:00Z", 610.0, 612.0))
    got = {e.ticker: e for e in venue.entries_for(call, call.recorded_utc, dt.date(2026, 11, 23), symbols, {"hyperliquid": fetch}, BASIS)}
    nvda = got["NVDA"]                                                                             # two quiet hours: the first candle inside six hours is the buy
    assert (nvda.entry_utc, nvda.entry_price, nvda.close_utc, nvda.close_price) == (ts("2026-11-21T12:00:00Z"), 128.5, ts("2026-11-23T21:00:00Z"), 131.07)
    assert nvda.ratio == pytest.approx(131.07 / 128.5)
    assert (got["SKH-USD"].close_utc, got["SKH-USD"].close_price) == (ts("2026-11-23T06:00:00Z"), 816.0)
    assert got["FIG"].venue == "none" and got["FIG"].ratio is None and "no venue market" in got["FIG"].basis
    assert got["MU"].venue == "none" and got["MU"].ratio is None and "within six hours of the buy" in got["MU"].basis      # its first candle came seven hours late
    assert venue.factors(list(got.values()), [call])[60] == pytest.approx(1 + 0.25 * (131.07 / 128.5 - 1) + 0.25 * (816 / 800 - 1))


def test_a_short_leg_gains_when_the_venue_price_falls_before_the_close():
    call = make_call(call_id=61, legs="AAA:0.5;BBB:-0.5")
    rows = [venue.Entry(61, "AAA", "hyperliquid", "a", ts("2026-01-03T10:00:00Z"), 100.0, ts("2026-01-05T21:00:00Z"), 104.0, "x"),
            venue.Entry(61, "BBB", "hyperliquid", "b", ts("2026-01-03T10:00:00Z"), 50.0, ts("2026-01-05T21:00:00Z"), 45.0, "x")]
    assert venue.factors(rows, [call]) == {61: pytest.approx(1 + 0.5 * 0.04 + 0.5 * 0.10)}
    assert venue.factors([], [call]) == {}


def test_the_book_gets_the_early_gain_on_the_entry_day_and_nothing_else_changes():
    days, st = small_store(6, AAA=[10, 10, 11, 12, 12, 12], BBB=[5, 5, 5, 5, 5, 6])
    first = make_call(call_id=1, published="2026-01-03T15:00:00Z", legs="AAA:1")                    # enters Mon 5 Jan
    second = make_call(call_id=2, published="2026-01-06T22:00:00Z", legs="BBB:1")                   # enters Wed 7 Jan
    plan = engine.plan([first, second], days)
    plain = engine.simulate(plan, st, 10_000.0)
    early = engine.simulate(plan, st, 10_000.0, early={2: 1.04})
    i = days.index(dt.date(2026, 1, 7))
    assert plain.equity[:i] == early.equity[:i]
    share = plain.equity[i] / 2                                                                     # two calls, equal parts of that close
    assert early.equity[i] - plain.equity[i] == pytest.approx(share * 0.04)
    assert early.call_pnl[i][2] == pytest.approx(share * 0.04) and early.n_trades == plain.n_trades
    assert early.holdings[2] == pytest.approx(plain.holdings[2] * 1.04) and early.holdings[1] == pytest.approx(plain.holdings[1])
    assert early.equity[-1] - plain.equity[-1] == pytest.approx(share * 0.04 * 6 / 5)               # the extra dollars then earn what the call earns
    assert engine.simulate(plan, st, 10_000.0, early={}).equity == plain.equity
    # a book that starts later buys the first call at its own start, not on the venue: no early gain for it
    late_book = engine.plan([first, second], days, not_before=dt.date(2026, 1, 6))
    assert engine.simulate(late_book, st, 10_000.0, early={1: 1.5}).equity == engine.simulate(late_book, st, 10_000.0).equity


def test_a_typed_line_is_refused():
    days, _ = small_store(8)
    call = make_call(call_id=60, published="2026-01-03T15:00:00Z", recorded="2026-01-06T15:10:00Z", legs="SPY:1")      # stamped Tue 6 Jan, enters Wed 7 Jan
    ok = venue.Entry(60, "SPY", "hyperliquid", "xyz:SP500", ts("2026-01-06T16:00:00Z"), 100.0, ts("2026-01-07T21:00:00Z"), 101.0, BASIS)
    venue.check([ok], [call], days, 49)
    swap = lambda **kw: [venue.Entry(**{**ok.__dict__, **kw})]                                      # noqa: E731
    with pytest.raises(ValueError, match="not an hourly open in the six hours from 2026-01-06T16:00:00Z"):
        venue.check(swap(entry_utc=ts("2026-01-06T15:00:00Z")), [call], days, 49)                   # before the stamp
    with pytest.raises(ValueError, match="not an hourly open in the six hours"):
        venue.check(swap(entry_utc=ts("2026-01-07T09:00:00Z")), [call], days, 49)                   # a better price picked the next morning
    with pytest.raises(ValueError, match="not within six hours before the rule's close at 2026-01-07T21:00:00Z"):
        venue.check(swap(close_utc=ts("2026-01-08T21:00:00Z")), [call], days, 49)
    with pytest.raises(ValueError, match="listed twice"):
        venue.check([ok, ok], [call], days, 49)
    with pytest.raises(ValueError, match="no such call or leg"):
        venue.check(swap(ticker="QQQ"), [call], days, 49)
    with pytest.raises(ValueError, match="two positive prices"):
        venue.check(swap(entry_price=0.0), [call], days, 49)
    with pytest.raises(ValueError, match="carries no times and no prices"):
        venue.check(swap(venue="none"), [call], days, 49)
    with pytest.raises(ValueError, match="two positive prices"):
        venue.check(swap(close_price=None), [call], days, 49)
    venue.check(swap(venue="none", entry_utc=None, entry_price=None, close_utc=None, close_price=None), [call], days, 49)   # the owner's way to give up on a leg
    unstamped = make_call(call_id=60, published="2026-01-03T15:00:00Z", legs="SPY:1")
    with pytest.raises(ValueError, match="has no stamp"):
        venue.check([ok], [unstamped], days, 49)
    venue.check([ok], [call], days, 60)                                                             # a backtest line is not held to the stamp


def test_a_call_is_looked_up_once_after_its_close_and_an_outage_writes_nothing(tmp_path):
    days, _ = small_store(8)
    (tmp_path / venue.SYMBOLS).write_text("ticker,venue,symbol\nSPY,hyperliquid,xyz:SP500\n", encoding="utf-8")
    call = make_call(call_id=60, published="2026-01-03T15:00:00Z", recorded="2026-01-06T15:10:00Z", legs="SPY:1")
    old = make_call(call_id=7, published="2026-01-03T15:00:00Z", recorded="2026-01-06T15:10:00Z", legs="SPY:1")       # a backtest call, even with a time on it: never looked up
    fetch = {"hyperliquid": candles(("xyz:SP500", "2026-01-06T16:00:00Z", 100.0, 100.5), ("xyz:SP500", "2026-01-07T20:00:00Z", 100.9, 101.0))}
    assert venue.update(tmp_path, [call, old], days, 49, ts("2026-01-07T21:04:00Z"), fetch) == []   # the close is not five minutes old yet
    assert venue.update(tmp_path, [call, old], days[:2], 49, ts("2026-01-09T01:07:00Z"), fetch) == []   # its entry session is not stored yet

    def down(symbol, start, end):
        raise OSError("connection reset")
    with pytest.raises(RuntimeError, match="venue: cannot download xyz:SP500 from hyperliquid for call 60.*Nothing was written"):
        venue.update(tmp_path, [call, old], days, 49, ts("2026-01-08T01:07:00Z"), {"hyperliquid": down})
    assert not (tmp_path / venue.ENTRIES).exists()
    added = venue.update(tmp_path, [call, old], days, 49, ts("2026-01-08T01:07:00Z"), fetch)
    assert [(e.call_id, e.entry_price, e.close_price) for e in added] == [(60, 100.0, 101.0)]
    assert (tmp_path / venue.ENTRIES).read_text() == (",".join(venue.COLUMNS) + "\n"
                                                      f"60,SPY,hyperliquid,xyz:SP500,2026-01-06T16:00:00Z,100,2026-01-07T21:00:00Z,101,{BASIS}\n")
    assert venue.update(tmp_path, [call, old], days, 49, ts("2026-01-08T07:07:00Z"), {"hyperliquid": down}) == []      # done: the venue is not asked again
    assert venue.load_entries(tmp_path) == added
    assert venue.not_looked_up([], [call, old], days, 49, days[2]) == [60] and venue.not_looked_up(added, [call, old], days, 49, days[2]) == []
    assert venue.not_looked_up([], [call, old], days, 49, days[1]) == []                            # it has not entered by then


def test_the_backtest_lines_on_file():
    cfg = cli.load_config(ROOT / "strategies.toml")
    calls = ledger.load_calls(ROOT / "calls.csv"); store = prices.load_prices(ROOT / "data")
    rows = venue.load_entries(ROOT / "data")
    venue.check(rows, calls, store.sessions(), cfg["recovered_calls_through"])
    old = [e for e in rows if e.call_id <= cfg["recovered_calls_through"]]
    assert len({e.call_id for e in old}) == 19 and len(old) == 25 and all(e.ratio is not None and e.basis.startswith("backtest:") for e in old)
    by_id = {c.call_id: c for c in calls}
    assert all(e.entry_utc >= by_id[e.call_id].published_utc + dt.timedelta(hours=1) for e in old)  # never before the episode was out
    f = venue.factors(old, calls)
    assert f[49] == pytest.approx(1.0553, abs=2e-4) and f[32] == pytest.approx(1.0040, abs=2e-4) and f[17] == pytest.approx(0.9723, abs=2e-4)
    seoul = [e for e in old if e.ticker in prices.SYNTHETIC]
    assert len(seoul) == 2 and all(e.close_utc.hour == 6 for e in seoul)                            # compared at the Seoul close, not New York's
    symbols = venue.load_symbols(ROOT / "data")
    assert symbols["GOOG"] == ("hyperliquid", "xyz:GOOGL") and symbols["BTC-USD"] == ("binance", "BTCUSDT") and {v for v, _ in symbols.values()} <= set(venue.FETCHERS)


def test_the_early_strategy_in_the_backtest(tmp_path):
    cfg = cli.load_config(ROOT / "strategies.toml")
    calls = [c for c in ledger.load_calls(ROOT / "calls.csv") if c.call_id <= cfg["recovered_calls_through"]]
    shutil.copy(ROOT / "data" / prices.SNAPSHOT, tmp_path / prices.SNAPSHOT)
    store = prices.load_prices(tmp_path)                                                            # the frozen snapshot: these figures never move
    lines = [e for e in venue.load_entries(ROOT / "data") if e.call_id <= cfg["recovered_calls_through"]]
    built = cli.build(calls, store, cfg, dt.datetime(2026, 11, 19, 7, 0, tzinfo=UTC), entries=lines)
    plain, early = built["daily"]["allin_12m"], built["daily"]["allin_247"]
    assert plain["date"].to_list() == early["date"].to_list() and plain["n_trades"].to_list() == early["n_trades"].to_list()
    at_mark = lambda t: 10_000 + t.filter(pl.col("date") <= MARK)["pnl"].sum()                      # noqa: E731
    assert at_mark(plain) == pytest.approx(24_634.14, abs=0.5)
    assert at_mark(early) == pytest.approx(EARLY_AT_MARK, abs=1.0)
    differ = plain.join(early, on="date").filter((pl.col("pnl") - pl.col("pnl_right")).abs() > 1e-9)["date"].to_list()
    assert differ[0] == dt.date(2021, 5, 24)                                                        # the first call with a venue price: Bitcoin, May 2021
    nothing = cli.build(calls, store, cfg, dt.datetime(2026, 11, 19, 7, 0, tzinfo=UTC))["daily"]
    assert nothing["allin_247"].equals(nothing["allin_12m"])                                  # with no venue lines the two are the same book


EARLY_AT_MARK = 26_107.82    # against 24,634.14 for the first strategy: the calls that had a venue market, bought one hour after their episode


def test_the_job_buys_a_new_call_early_and_records_it_once(tmp_path, monkeypatch):
    """The command the job runs over the first sessions, with a stamped call that enters on Monday 23 Nov 2026."""
    cfg = cli.load_config(ROOT / "strategies.toml")
    frozen = tmp_path / "frozen"; frozen.mkdir(); shutil.copy(ROOT / "data" / prices.SNAPSHOT, frozen / prices.SNAPSHOT)
    store = prices.load_prices(frozen)
    data = tmp_path / "data"; data.mkdir(); shutil.copy(ROOT / "data" / prices.SNAPSHOT, data / prices.SNAPSHOT)
    shutil.copy(ROOT / "data" / venue.SYMBOLS, data / venue.SYMBOLS)
    lines = (ROOT / "calls.csv").read_text(encoding="utf-8").splitlines(keepends=True)[:1 + cfg["recovered_calls_through"]]
    calls_file = tmp_path / "calls.csv"; calls_file.write_text("".join(lines), encoding="utf-8")
    out = tmp_path / "results"
    base = ["--data", str(data), "--calls", str(calls_file), "--out", str(out)]
    paper = lambda sid: pl.read_parquet(out / sid / "daily.parquet").filter(pl.col("mode") == "paper")      # noqa: E731
    at = lambda day, hour=1: dt.datetime.combine(day + dt.timedelta(days=1), dt.time(hour, 7), UTC)       # noqa: E731
    thu, fri, mon, tue = (dt.date(2026, 11, n) for n in (19, 20, 23, 24))
    asked = []

    def hyperliquid(symbol, start, end):
        asked.append(symbol)
        return candles(("xyz:NVDA", "2026-11-21T10:00:00Z", 128.5, 128.6), ("xyz:NVDA", "2026-11-23T20:00:00Z", 131.0, 131.07))(symbol, start, end)
    fetchers = {"hyperliquid": hyperliquid}
    moves = {thu: 1.0, fri: 1.0}
    for day in (thu, fri):
        assert cli.main(["run", "--fetch", *base], now=at(day), fetch=stand_in_vendor(store, {d: m for d, m in moves.items() if d <= day}), venue_fetch=fetchers) == 0
    assert asked == [] and not (data / venue.ENTRIES).exists()                                      # nothing has entered yet: the venue is not asked
    assert paper("allin_247").equals(paper("allin_12m"))

    # Saturday: the call reaches main and the job stamps it
    calls_file.write_text("".join(lines) + "50,guid-n,2026-11-20T22:14:00Z,Jason,Nvidia,NVDA:1,active,2026-11-21T09:52:03Z,,,test\n", encoding="utf-8")
    (data / cli.STAMPS).write_text("call_id,recorded_utc\n50,2026-11-21T09:52:03Z\n", encoding="utf-8")

    def down(symbol, start, end):
        raise OSError("connection reset")
    moves[mon] = {"*": 1.0, "NVDA": 1.03}
    with pytest.raises(RuntimeError, match="venue: cannot download xyz:NVDA from hyperliquid for call 50"):
        cli.main(["run", "--fetch", *base], now=at(mon), fetch=stand_in_vendor(store, moves), venue_fetch={"hyperliquid": down})
    assert json.loads((out / "index.json").read_text())["data_through"] == str(fri)                 # an outage publishes nothing, for either strategy

    assert cli.main(["run", "--fetch", *base], now=at(mon), fetch=stand_in_vendor(store, moves), venue_fetch=fetchers) == 0
    line = f"50,NVDA,hyperliquid,xyz:NVDA,2026-11-21T10:00:00Z,128.5,2026-11-23T21:00:00Z,131.07,{BASIS}\n"
    assert (data / venue.ENTRIES).read_text() == ",".join(venue.COLUMNS) + "\n" + line and asked == ["xyz:NVDA"]
    plain, early = paper("allin_12m"), paper("allin_247")
    assert plain["date"].to_list() == early["date"].to_list() == [thu, fri, mon] and plain["n_trades"].to_list() == early["n_trades"].to_list() == [19, 0, 1]
    share = plain["capital"][2] / 20                                                                # twenty calls, equal parts of Monday's close
    gain = share * (131.07 / 128.5 - 1)
    assert early["pnl"][2] - plain["pnl"][2] == pytest.approx(gain, abs=1e-6) and gain == pytest.approx(10.02, abs=0.01)
    assert plain["pnl"][2] == pytest.approx(10_000 / 19 * 0.03, abs=1e-6)                           # the first strategy is untouched: its one Nvidia call rose 3%
    manifest = json.loads((out / "allin_247" / "manifest.json").read_text())
    assert manifest["entry"] == "venue" and manifest["role"] == "monitor" and json.loads((out / "allin_12m" / "manifest.json").read_text())["entry"] == "close"

    # a second run the same night changes nothing and does not ask the venue again
    assert cli.main(["run", "--fetch", *base], now=at(mon, 7), fetch=stand_in_vendor(store, moves), venue_fetch={"hyperliquid": down}) == 0
    assert paper("allin_247").equals(early) and (data / venue.ENTRIES).read_text().endswith(line)

    # from the next day both hold the same thing; the early book has a little more of Nvidia
    moves[tue] = {"*": 1.0, "NVDA": 1.10}
    assert cli.main(["run", "--fetch", *base], now=at(tue), fetch=stand_in_vendor(store, moves), venue_fetch={"hyperliquid": down}) == 0
    assert paper("allin_247")["pnl"][3] - paper("allin_12m")["pnl"][3] == pytest.approx(gain * 0.10, abs=1e-6)
    assert paper("allin_12m")["pnl"][3] == pytest.approx(2 * share * 0.10, abs=1e-6)

    # a buy time typed by hand stops the run before anything is computed
    (data / venue.ENTRIES).write_text((data / venue.ENTRIES).read_text().replace("2026-11-21T10:00:00Z,128.5", "2026-11-22T22:00:00Z,120"), encoding="utf-8")
    with pytest.raises(ValueError, match="call 50 NVDA: bought at 2026-11-22T22:00:00Z, which is not an hourly open in the six hours from 2026-11-21T10:00:00Z"):
        cli.main(["run", *base], now=at(tue, 7))
