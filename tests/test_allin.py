"""Tests: the calendar rule, the recovered backtest figures, the paper book, the live price update and the published contract."""
import csv
import dataclasses
import datetime as dt
import gzip
import json
import pathlib
import shutil

import polars as pl
import pytest

from allin import cli, contract, engine, episodes, ledger, prices

ROOT = pathlib.Path(__file__).resolve().parents[1]
MARK = dt.date(2026, 9, 25)          # the day the recovered backtest marked its open calls
UTC = dt.timezone.utc


FROZEN_THROUGH = dt.date(2026, 11, 18)      # last day of the price snapshot


@pytest.fixture(scope="module")
def cfg():
    return cli.load_config(ROOT / "strategies.toml")


@pytest.fixture(scope="module")
def ledger_all():
    """Every line of the live ledger, including calls added after these tests were written."""
    return ledger.load_calls(ROOT / "calls.csv")


@pytest.fixture(scope="module")
def calls(ledger_all, cfg):
    """The backtest ledger: it never changes, so figures computed from it can be pinned."""
    return [c for c in ledger_all if c.call_id <= cfg["recovered_calls_through"]]


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    """The frozen price snapshot alone, without the rows the daily job keeps appending."""
    folder = tmp_path_factory.mktemp("frozen")
    shutil.copy(ROOT / "data" / prices.SNAPSHOT, folder / prices.SNAPSHOT)
    frozen = prices.load_prices(folder)
    assert frozen.sessions()[-1] == FROZEN_THROUGH
    return frozen


@pytest.fixture(scope="module")
def backtest_ledger_text(cfg):
    """calls.csv as it was when the backtest ledger was complete: the header and the calls up to the last backtest id."""
    lines = (ROOT / "calls.csv").read_text(encoding="utf-8").splitlines(keepends=True)
    return "".join(lines[:1 + cfg["recovered_calls_through"]])


def make_call(call_id=900, published="2026-10-12T15:49:00Z", legs="SPY:1", recorded="", forced="", speaker="Jason", status="active"):
    return ledger.Call(call_id, "guid", ledger._ts(published), speaker, "test", ledger.parse_legs(legs), status, ledger._ts(recorded), None,
                       dt.date.fromisoformat(forced) if forced else None, "")


def weekdays(start, n):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


# ---------- ledger and calendar rule ----------
def test_ledger_shape(ledger_all, calls):
    assert [c.call_id for c in sorted(ledger_all, key=lambda c: c.call_id)] == list(range(1, len(ledger_all) + 1))     # ids are consecutive
    assert len(calls) == 49
    assert [c.call_id for c in calls if c.status == "excluded"] == [41]            # GEODNET: no price history
    for c in ledger_all:
        assert abs(sum(abs(w) for _, w in c.legs) - 1) < 1e-3


def test_every_active_ticker_has_prices(calls, store):
    missing = {t for c in calls if c.status == "active" for t, _ in c.legs if not store.has(t)}
    assert not missing


def test_entry_is_the_first_session_after_the_new_york_publication_date():
    s = weekdays(dt.date(2026, 10, 12), 10)                                         # Mon 12 Oct .. Fri 23 Oct
    assert ledger.entry_date(make_call(published="2026-10-12T15:49:00Z"), s) == dt.date(2026, 10, 13)   # during Monday's session: Tuesday
    assert ledger.entry_date(make_call(published="2026-10-17T01:51:00Z"), s) == dt.date(2026, 10, 19)   # Friday evening in New York: Monday
    assert ledger.entry_date(make_call(published="2026-10-13T03:30:00Z"), s) == dt.date(2026, 10, 13)   # still Monday in New York: Tuesday
    assert ledger.entry_date(make_call(published="2026-10-23T15:00:00Z"), s) is None                    # next session not stored yet


def test_exit_is_one_calendar_year_later_rolled_to_a_session(store):
    s = store.sessions()
    assert ledger.exit_date(dt.date(2025, 4, 3), s) == dt.date(2026, 4, 6)          # 3 Apr 2026 is Good Friday
    assert ledger.exit_date(dt.date(2021, 4, 19), s) == dt.date(2022, 4, 19)
    assert ledger.exit_date(s[-1], s) is None                                       # not reached yet
    assert ledger.plus_year(dt.date(2024, 2, 29)) == dt.date(2025, 2, 28)


def test_a_call_recorded_late_enters_late():
    s = weekdays(dt.date(2026, 10, 12), 15)
    late = make_call(published="2026-10-12T15:49:00Z", recorded="2026-10-21T09:00:00Z")
    assert ledger.entry_date(late, s) == dt.date(2026, 10, 22)
    same_day = make_call(published="2026-10-12T15:49:00Z", recorded="2026-10-12T19:00:00Z")
    assert ledger.entry_date(same_day, s) == dt.date(2026, 10, 13)


def test_forced_exit_closes_early(store):
    s = store.sessions()
    assert ledger.exit_date(dt.date(2025, 1, 6), s, forced=dt.date(2025, 7, 4)) == dt.date(2025, 7, 7)   # 4 Jul is a holiday
    assert ledger.exit_date(dt.date(2025, 1, 6), s, forced=dt.date(2027, 1, 1)) == dt.date(2026, 1, 6)   # the rule exit comes first


def test_ledger_check_refuses_impossible_timestamps(cfg):
    now = dt.datetime(2026, 12, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="recorded_utc is in the future"):
        cli.check_ledger([make_call(published="2026-11-21T02:00:00Z", recorded="2026-12-05T00:00:00Z")], cfg, now)
    with pytest.raises(ValueError, match="before the episode"):
        cli.check_ledger([make_call(published="2026-11-21T02:00:00Z", recorded="2026-11-20T00:00:00Z")], cfg, now)
    with pytest.raises(ValueError, match="published_utc is in the future"):
        cli.check_ledger([make_call(published="2026-12-02T02:00:00Z")], cfg, now)
    cli.check_ledger([make_call(published="2026-11-21T02:00:00Z", recorded="2026-11-22T00:00:00Z"), make_call(call_id=901, published="2026-11-21T02:00:00Z")], cfg, now)


def test_a_new_call_is_on_file_but_not_traded_until_it_is_stamped(calls, cfg):
    assert cfg["recovered_calls_through"] == 49
    assert not any(cli.is_pending(c, cfg) for c in calls if c.call_id <= 49)          # the backtest ledger enters by episode date
    new = make_call(call_id=50, published="2026-11-21T02:00:00Z")
    assert cli.is_pending(new, cfg)
    assert not cli.is_pending(dataclasses.replace(new, status="excluded"), cfg)
    assert not cli.is_pending(dataclasses.replace(new, recorded_utc=dt.datetime(2026, 11, 22, 9, 0, tzinfo=UTC)), cfg)


def test_stamping_fills_only_new_empty_lines_and_changes_nothing_else(tmp_path, cfg, backtest_ledger_text):
    path = tmp_path / "calls.csv"
    original = backtest_ledger_text
    extra = ("50,guid-a,2026-11-21T02:35:00Z,Friedberg,Uranium,URA:1,active,,,,\"Says he is buying; a note, with a comma.\"\n"
             "51,guid-b,2026-11-21T02:35:00Z,Jason,Something private,SPY:1,excluded,,,,\n"
             "52,guid-c,2026-11-23T20:00:00Z,Chamath,Copper,CPER:1,active,2026-11-24T05:00:00Z,,,\n")
    path.write_text(original + extra, encoding="utf-8")
    when = dt.datetime(2026, 11, 22, 8, 15, 30, tzinfo=UTC)                        # Sunday morning in Dubai, still Sunday in New York
    assert cli.stamp_calls(path, cfg, when) == [50]
    after = path.read_text(encoding="utf-8")
    assert after == original + extra.replace("URA:1,active,,", "URA:1,active,2026-11-22T08:15:30Z,")
    assert cli.stamp_calls(path, cfg, when + dt.timedelta(days=1)) == []          # a stamp is never moved
    assert path.read_text(encoding="utf-8") == after
    stamped = {c.call_id: c for c in ledger.load_calls(path)}
    sessions = weekdays(dt.date(2026, 11, 16), 15)
    assert ledger.entry_date(stamped[50], sessions) == dt.date(2026, 11, 23)       # merged on the weekend: bought at Monday's close
    assert ledger.entry_date(stamped[52], sessions) == dt.date(2026, 11, 25)       # stamped just after midnight New York time on Tuesday: Wednesday
    assert stamped[46].recorded_utc is None                                        # the backtest ledger is left alone


def test_a_pending_call_does_not_reach_the_books(tmp_path, capsys, ledger_all):
    """Run the real pipeline twice, once with an unstamped line added: the published days are identical.
    The line is dated so that trading it by episode date would have changed days that are already in the record."""
    new_id = max(c.call_id for c in ledger_all) + 1
    path = tmp_path / "calls.csv"
    path.write_text((ROOT / "calls.csv").read_text(encoding="utf-8") + f"{new_id},guid-a,2026-11-10T15:00:00Z,Friedberg,Uranium,URA:1,active,,,,\n", encoding="utf-8")
    assert cli.main(["run", "--calls", str(path), "--out", str(tmp_path / "out")]) == 0
    assert f"call {new_id} (Friedberg: Uranium) is on file and not traded yet" in capsys.readouterr().out
    with_pending = pl.read_parquet(tmp_path / "out" / "allin_12m" / "daily.parquet")
    assert cli.main(["run", "--out", str(tmp_path / "plain")]) == 0
    assert with_pending.equals(pl.read_parquet(tmp_path / "plain" / "allin_12m" / "daily.parquet"))
    assert json.loads((tmp_path / "out" / "allin_12m" / "manifest.json").read_text())["calls_in_ledger"] == len(ledger_all) + 1


FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>show</title>
<item><title>Weekly   show &amp; more</title><guid isPermaLink="false">g-new-2</guid><link>https://example.com/2</link>
  <pubDate>Fri, 20 Nov 2026 21:35:00 -0500</pubDate><enclosure url="https://example.com/2.mp3" length="1" type="audio/mpeg"/></item>
<item><title>An interview</title><guid>g-new-1</guid><link>https://example.com/1</link><pubDate>Thu, 19 Nov 2026 23:10:00 +0000</pubDate></item>
<item><title>A retitled old episode</title><guid>22b845c5-e5be-4dd1-b4dd-7f3843120d12</guid><pubDate>Tue, 17 Nov 2026 21:43:00 +0000</pubDate></item>
</channel></rss>"""


def test_catalogue_appends_new_episodes_and_never_rewrites(tmp_path):
    path = tmp_path / "episodes.csv"; shutil.copy(ROOT / "data" / "episodes.csv", path)
    before = path.read_text(encoding="utf-8")
    rows = episodes.parse_feed(FEED)
    assert [r[1] for r in rows] == ["22b845c5-e5be-4dd1-b4dd-7f3843120d12", "g-new-1", "g-new-2"]      # oldest first
    added = episodes.update_catalogue(path, rows)
    assert added == [("2026-11-19T23:10:00Z", "g-new-1", "An interview", "https://example.com/1", ""),
                     ("2026-11-21T02:35:00Z", "g-new-2", "Weekly show & more", "https://example.com/2", "https://example.com/2.mp3")]
    after = path.read_text(encoding="utf-8")
    assert after.startswith(before) and after.count("\n") == before.count("\n") + 2                    # the stored title is kept
    assert episodes.update_catalogue(path, rows) == []
    feed_file = tmp_path / "feed.xml"; feed_file.write_bytes(FEED)
    assert cli.main(["episodes", "--data", str(tmp_path), "--feed", str(feed_file)]) == 0
    assert path.read_text(encoding="utf-8") == after


def test_feed_parser_refuses_what_it_should_not_read():
    with pytest.raises(ValueError, match="document type or entities"):
        episodes.parse_feed(b'<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY a "aaaa">]><rss><channel><item><title>&a;</title></item></channel></rss>')
    with pytest.raises(ValueError, match="missing"):
        episodes.parse_feed(b"<rss><channel><item><title>no guid</title><pubDate>Thu, 19 Nov 2026 23:10:00 +0000</pubDate></item></channel></rss>")
    with pytest.raises(ValueError, match="duplicate"):
        episodes.parse_feed(FEED.replace(b"g-new-2", b"g-new-1"))
    with pytest.raises(ValueError, match="no time zone"):
        episodes.parse_feed(FEED.replace(b"23:10:00 +0000", b"23:10:00 -0000"))


def test_screening_log_agrees_with_the_catalogue_and_the_ledger(ledger_all):
    catalogue = {r["guid"]: r for r in episodes.load_catalogue(ROOT / "data" / "episodes.csv")}
    by_id = {c.call_id: c for c in ledger_all}
    with open(ROOT / "data" / "screening_log.csv", newline="", encoding="utf-8") as f:
        log = list(csv.DictReader(f))
    assert len({r["guid"] for r in log}) == len(log) >= 17
    start = cli.load_config(ROOT / "strategies.toml")["screening_start"]
    for r in log:
        assert catalogue[r["guid"]]["published_utc"] == r["published_utc"] and catalogue[r["guid"]]["title"] == r["title"]
        assert r["published_utc"][:10] >= start and r["read_from"] in ("transcript", "summary")
        assert (r["read_from"] == "transcript") == bool(r["transcript_url"])
        for cid in filter(None, r["calls_added"].split(";")):
            assert by_id[int(cid)].episode_guid == r["guid"]
    logged = {int(i) for r in log for i in filter(None, r["calls_added"].split(";"))}
    assert {46, 47, 48, 49} <= logged <= set(by_id)                                # the four calls found on 19 Nov 2026 are in the log


# ---------- the recovered backtest ----------
NEAR = {20, 27, 29, 32}     # Magnificent Seven, Bittensor, Tesla, memory basket: within 0.35 points; the rest to the printed decimal


def test_recovered_call_returns(calls, store):
    s = store.sessions()
    checked = 0
    for c in calls:
        if c.status != "active" or c.recovered_return_pct is None:
            continue
        entry = ledger.entry_date(c, s); leave = ledger.exit_date(entry, s)
        day = leave if leave is not None and leave <= MARK else MARK
        mine = (engine.call_value(c, entry, day, store) - 1) * 100
        tol = 0.35 if c.call_id in NEAR else 0.051
        assert abs(mine - c.recovered_return_pct) <= tol, (c.call_id, c.call, mine, c.recovered_return_pct)
        checked += 1
    assert checked == 44


def test_recovered_portfolio(calls, store):
    res = engine.simulate(engine.plan([c for c in calls if c.call_id <= 45], store.sessions()), store, 10_000.0, end=MARK)
    assert res.dates[0] == dt.date(2021, 4, 19)
    assert res.equity[-1] == pytest.approx(24_634.14, abs=0.5)         # 25,008 in the original, which also had GEODNET
    st = engine.stats(res.equity)
    assert st["max_dd_pct"] == pytest.approx(-54.1, abs=0.05)
    closed = [p for p in res.positions.values() if p.exit is not None and p.exit <= MARK]
    assert len(closed) == 26
    assert sum(engine.call_value(p.call, p.entry, p.exit, store) > 1 for p in closed) == 19
    spy = 10_000 * store.on("SPY", MARK) / store.on("SPY", dt.date(2021, 4, 19))
    assert spy == pytest.approx(19_991, abs=1.0)


def reference_equity(positions, store, cash, end):
    """The same portfolio written another way: units of each call's value index instead of running dollars."""
    pos = {p.call.call_id: p for p in positions if p.entry is not None and p.entry <= end}
    start = min(p.entry for p in pos.values())
    units, money, out = {}, cash, []
    for d in [x for x in store.sessions() if start <= x <= end]:
        value = {k: engine.call_value(pos[k].call, pos[k].entry, d, store) for k in units}
        total = money + sum(units[k] * value[k] for k in units)
        leaving = {k for k in units if pos[k].exit == d}; joining = [k for k, p in pos.items() if p.entry == d]
        if leaving or joining:
            live = [k for k in units if k not in leaving] + joining
            units = {k: total / len(live) / (value[k] if k in value else 1.0) for k in live}
            money = 0.0 if live else total
        out.append(total)
    return out


def test_engine_matches_an_independent_implementation(calls, store):
    plan = engine.plan(calls, store.sessions())
    res = engine.simulate(plan, store, 10_000.0)
    ref = reference_equity(plan, store, 10_000.0, res.dates[-1])
    assert len(ref) == len(res.equity)
    assert max(abs(a / b - 1) for a, b in zip(res.equity, ref)) < 1e-9


def test_daily_attribution_adds_up(calls, store):
    res = engine.simulate(engine.plan(calls, store.sessions()), store, 10_000.0)
    for i in range(1, len(res.equity)):
        assert sum(res.call_pnl[i].values()) == pytest.approx(res.equity[i] - res.equity[i - 1], abs=1e-6)
    assert sum(res.holdings.values()) == pytest.approx(res.equity[-1], rel=1e-12)


# ---------- the paper book ----------
def test_paper_book_starts_in_cash_and_buys_what_is_open(calls, store):
    s = store.sessions(); start = dt.date(2026, 11, 2)
    plan = engine.plan(calls, s, not_before=start)
    res = engine.simulate(plan, store, 10_000.0)
    assert res.dates[0] == start and res.equity[0] == 10_000.0
    full = {p.call.call_id: p for p in engine.plan(calls, s)}
    open_then = {k for k, p in full.items() if p.entry <= start and (p.exit is None or p.exit > start)}
    assert res.n_trades[0] == res.n_active[0] == len(open_then)
    for k, p in res.positions.items():
        assert p.exit == full[k].exit and p.rule_entry == full[k].entry          # it leaves on the original day
        assert p.entry == max(full[k].entry, start)
    assert not {k for k, p in full.items() if p.exit is not None and p.exit <= start} & set(res.positions)


def test_paper_book_tracks_the_backtest_book_in_returns(calls, store):
    """From any start day the two books hold the same calls, so their daily returns differ only through the weights."""
    s = store.sessions(); start = dt.date(2026, 11, 9)
    paper = engine.simulate(engine.plan(calls, s, not_before=start), store, 10_000.0, end=FROZEN_THROUGH)
    back = engine.simulate(engine.plan(calls, s), store, 10_000.0, end=FROZEN_THROUGH)
    i = back.dates.index(start)
    a = paper.equity[-1] / paper.equity[0] - 1; b = back.equity[-1] / back.equity[i] - 1
    assert abs(a - b) < 0.01


def test_a_new_call_never_changes_days_already_published(calls, store):
    s = store.sessions()
    before = engine.simulate(engine.plan(calls, s), store, 10_000.0)
    late = make_call(published="2026-11-10T23:03:00Z", recorded=f"{s[-1]}T22:00:00Z", legs="SPY:1")
    plan = engine.plan([*calls, late], s)
    assert [p for p in plan if p.call.call_id == 900][0].entry is None      # recorded on the last stored day: enters on the next session
    after = engine.simulate(plan, store, 10_000.0)
    assert after.equity == before.equity
    backdated = make_call(published="2026-11-10T23:03:00Z", legs="SPY:1")
    assert engine.simulate(engine.plan([*calls, backdated], s), store, 10_000.0).equity != before.equity


def test_a_call_that_loses_everything_is_closed_at_nothing():
    days = weekdays(dt.date(2026, 1, 5), 8)
    store_ = prices.Prices({"SPY": {d: 100.0 * 1.01 ** i for i, d in enumerate(days)}, "UP": {d: 10.0 * 1.3 ** i for i, d in enumerate(days)}})
    long_ = make_call(call_id=1, published="2026-01-03T15:00:00Z", legs="SPY:1")
    short = make_call(call_id=2, published="2026-01-03T15:00:00Z", legs="UP:-1")             # loses 30% of the stake a day, then more
    res = engine.simulate(engine.plan([long_, short], days), store_, 10_000.0)
    assert res.wiped == {2: days[3]}                                                         # 1.3 ** 3 = 2.2: the short has more than doubled
    assert res.n_active == [2, 2, 2, 1, 1, 1, 1, 1] and res.n_trades[3] == 1
    assert res.equity[3] == pytest.approx(5_000 * 1.01 ** 3)                                 # the short's half is gone, not more than gone
    assert res.equity[-1] == pytest.approx(5_000 * 1.01 ** 7)                                # the rest carries on alone and the short never returns
    assert all(v == v and v > 0 for v in res.equity) and set(res.holdings) == {1}
    assert sum(sum(m.values()) for m in res.call_pnl) == pytest.approx(res.equity[-1] - 10_000)


def test_a_forced_exit_on_or_before_the_entry_means_the_call_is_never_held(store):
    s = store.sessions()
    gone = make_call(published="2026-10-12T15:49:00Z", forced="2026-10-13")                  # the ticker stopped trading on the entry day
    assert [p for p in engine.plan([gone], s) if p.call.call_id == 900] == []
    later = make_call(published="2026-10-12T15:49:00Z", forced="2026-10-20")
    assert engine.plan([later], s)[0].exit == dt.date(2026, 10, 20)


def test_a_call_can_enter_during_a_long_holiday_abroad(calls, store):
    """Seoul was closed from 3 to 9 October 2025. A call entering on the 8th is bought at the last Seoul close, six days old."""
    rows = [d for d in store._dates["SKH-USD"] if dt.date(2025, 10, 1) <= d <= dt.date(2025, 10, 10)]
    assert rows == [dt.date(2025, 10, 1), dt.date(2025, 10, 2), dt.date(2025, 10, 10)]
    c = make_call(published="2025-10-07T15:00:00Z", legs="MU:0.5;SKH-USD:0.5")
    p = engine.plan([c], store.sessions())[0]
    assert p.entry == dt.date(2025, 10, 8) and engine.call_value(c, p.entry, p.entry, store) == 1.0
    engine.simulate(engine.plan([*calls, c], store.sessions()), store, 10_000.0, end=dt.date(2025, 10, 31))


def small_store(n=8, **extra):
    """A toy price store: SPY on n weekdays from Mon 5 Jan 2026, plus whatever series are passed (a list of levels or a dict by date)."""
    days = weekdays(dt.date(2026, 1, 5), n)
    series = {"SPY": {d: 100.0 + i for i, d in enumerate(days)}}
    for name, levels in extra.items():
        key = name.replace("_USD", "-USD")
        series[key] = levels if isinstance(levels, dict) else {d: v for d, v in zip(days, levels) if v is not None}
    return days, prices.Prices(series)


def test_a_day_needs_its_own_price_for_us_symbols_and_crypto_but_not_for_foreign_shares():
    days, st = small_store(6, AAA=[10, 11, 12, 13, None, None], BTC_USD=[1, 2, 3, 4, 5, None], SKH_USD=[7, 7, 7, None, None, None])
    plan = lambda legs, **kw: engine.plan([make_call(published="2026-01-03T15:00:00Z", legs=legs, **kw)], days)
    assert engine.complete_through(plan("SPY:1"), st) == days[5]
    assert engine.complete_through(plan("AAA:1"), st) == days[3]                    # a US symbol with no bar that day holds publication back
    assert engine.complete_through(plan("BTC-USD:1"), st) == days[4]                # so does a cryptocurrency: it trades every day
    assert engine.complete_through(plan("SKH-USD:1"), st) == days[5]                # a Seoul share is carried over its holidays
    assert engine.complete_through(plan("AAA:1", forced="2026-01-12"), st) == days[5]   # and so is a ticker whose call has a forced exit
    assert prices.us_listed("BRK-B") and prices.needs_own_day("BRK-B") and not prices.us_listed("000660.KS")
    assert prices.needs_own_day("BTC-USD") and not prices.needs_own_day("SKH-USD") and not prices.us_listed("BTC-USD")


def test_a_ticker_that_stops_trading_stalls_the_job_until_its_call_gets_a_forced_exit():
    days, st = small_store(8, AAA=[10, 11, 12, None, None, None, None, None])       # AAA's last trade is on the third day
    long_ = make_call(call_id=1, published="2026-01-03T15:00:00Z", legs="SPY:1")
    dead = make_call(call_id=2, published="2026-01-03T15:00:00Z", legs="AAA:1")
    through = engine.complete_through(engine.plan([long_, dead], days), st)
    assert through == days[2]
    with pytest.raises(RuntimeError, match="5 sessions cannot be published"):
        cli.check_not_stalled(st, through)
    cli.check_not_stalled(st, days[4])                                              # three sessions behind is still tolerated
    closed = dataclasses.replace(dead, forced_exit=days[3])                         # the owner closes it on the first unpublished day
    plan = engine.plan([long_, closed], days)
    assert engine.complete_through(plan, st) == days[7]
    res = engine.simulate(plan, st, 10_000.0)
    assert res.n_active == [2, 2, 2, 1, 1, 1, 1, 1] and res.n_trades[3] == 1
    assert res.call_pnl[3][2] == 0.0                                                # closed at the last price it had
    need, optional = cli.tickers_to_update(plan, st, "SPY")
    assert "AAA" not in need                                                        # closed and valued: nothing more to download


def test_the_download_list_covers_open_and_coming_calls_only():
    days, st = small_store(8, AAA=[10] * 8, BBB=[10] * 8, CCC=[10] * 8, DDD=[10] * 7 + [None])
    open_ = make_call(call_id=1, published="2026-01-03T15:00:00Z", legs="AAA:1")
    coming = make_call(call_id=2, published="2026-01-14T22:00:00Z", legs="BBB:1")                  # enters after the last stored session
    over = make_call(call_id=3, published="2026-01-03T15:00:00Z", legs="CCC:1", forced="2026-01-08")
    ends_today = make_call(call_id=4, published="2026-01-03T15:00:00Z", legs="CCC:1", forced=str(days[-1]))
    unpriced_end = make_call(call_id=5, published="2026-01-03T15:00:00Z", legs="DDD:1", forced=str(days[-1]))
    plan = engine.plan([open_, coming, over, ends_today, unpriced_end], days)
    need, optional = cli.tickers_to_update(plan, st, "SPY")
    assert need == ["AAA", "BBB", "DDD", "SPY"] and optional == {"DDD"}             # DDD is only needed by a call with a forced exit


def test_money_waits_in_cash_between_calls():
    days, st = small_store(8)
    first = make_call(call_id=1, published="2026-01-03T15:00:00Z", legs="SPY:1", forced=str(days[2]))
    second = make_call(call_id=2, published=f"{days[4]}T15:00:00Z", legs="SPY:1")                   # enters on the sixth day
    res = engine.simulate(engine.plan([first, second], days), st, 10_000.0)
    table = cli.daily_table(res, "backtest")
    assert res.n_active == [1, 1, 0, 0, 0, 1, 1, 1]
    assert table["capital"].to_list()[2:5] == [0.0, 0.0, 0.0] and table["pnl"].to_list()[3:6] == [0.0, 0.0, 0.0]
    assert res.equity[2] == res.equity[5] == pytest.approx(10_000 * 102 / 100)       # the gain of the first call is what the second starts with
    assert table["capital"][5] == pytest.approx(res.equity[5])


def test_the_paper_book_skips_a_call_that_leaves_on_its_first_day(store):
    s = store.sessions()
    call = make_call(published="2025-11-05T15:00:00Z", legs="SPY:1")                                # enters 6 Nov 2025, leaves 6 Nov 2026
    assert engine.plan([call], s)[0].exit == dt.date(2026, 11, 6)
    assert engine.plan([call], s, not_before=dt.date(2026, 11, 6)) == []                            # it would be bought and sold at the same close
    assert engine.plan([call], s, not_before=dt.date(2026, 11, 7)) == []
    held = engine.plan([call], s, not_before=dt.date(2026, 11, 5))[0]
    assert held.entry == dt.date(2026, 11, 5) and held.exit == dt.date(2026, 11, 6)


def test_ledger_and_statistics_reject_or_survive_degenerate_input():
    for bad in ("SPY:nan", "SPY:0;QQQ:1", "SPY:inf", "SPY", "SPY:0.4"):
        with pytest.raises(ValueError):
            ledger.parse_legs(bad)
    assert ledger.parse_legs("MSFT:0.5; GOOG:-0.5") == (("MSFT", 0.5), ("GOOG", -0.5))
    assert engine.stats([10_000.0])["sharpe"] is None and engine.stats([10_000.0, 10_100.0])["sharpe"] is None
    assert engine.stats([10_000.0, 10_100.0])["pnl"] == 100.0


def test_a_strategy_with_no_call_yet_publishes_nothing_and_breaks_nothing(tmp_path, calls, store, cfg):
    nobody = dict(cfg, strategy=[dict(cfg["strategy"][0]), {"id": "empty", "name": "Nobody", "calls": "all", "paper": True, "role": "monitor"}])
    cli.FILTERS["none"] = lambda c: False
    try:
        nobody["strategy"][1]["calls"] = "none"
        now = dt.datetime(2026, 11, 19, 7, 0, tzinfo=UTC)
        built = cli.build(calls, store, nobody, now)
        assert list(built["daily"]) == ["allin_12m"] and "empty" not in built["summary"]["strategy_id"].to_list()
        cli.publish(str(tmp_path / "out"), nobody, built, store, len(calls), now_utc=now)
        early = dict(cfg, paper_start=store.sessions()[0])                                          # no session before the paper start
        assert cli.build(calls[:1], store, early, now)["summary"].height == 0
    finally:
        del cli.FILTERS["none"]


# ---------- live price update ----------
def write_snapshot(folder, series):
    folder.mkdir(parents=True, exist_ok=True)
    with gzip.open(folder / prices.SNAPSHOT, "wt", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n"); w.writerow(("date", "ticker", "tr"))
        for t, rows in series.items():
            for d, v in rows:
                w.writerow((d.isoformat(), t, v))


D = [dt.date(2026, 11, 16) + dt.timedelta(days=i) for i in range(5)]          # Mon 16 .. Fri 20 Nov 2026


def test_live_update_chains_ratios_and_never_rewrites(tmp_path):
    write_snapshot(tmp_path, {"SPY": [(D[0], 100.0), (D[1], 101.0), (D[2], 102.0)], "AAA": [(D[0], 50.0), (D[1], 51.0), (D[2], 49.0)]})
    vendor = {"SPY": [(D[0], 100, 100.0), (D[1], 101, 101.0), (D[2], 102, 102.0), (D[3], 103, 103.0), (D[4], 99, 99.0)],
              # the vendor has since restated AAA's adjusted prices (a dividend): levels halve, ratios stay
              "AAA": [(D[0], 50, 25.0), (D[1], 51, 25.5), (D[2], 49, 24.5), (D[3], 52, 26.0), (D[4], 60, 30.0)]}
    asked = []

    def fetch(symbol, start):
        asked.append((symbol, start)); return vendor[symbol]
    now = dt.datetime(2026, 11, 20, 12, 0, tzinfo=UTC)               # Friday midday: Thursday's bar is final, Friday's is not
    added = prices.update_live(tmp_path, ["AAA", "SPY"], now, fetch)
    assert added == [(D[3], "AAA", pytest.approx(49.0 * 26.0 / 24.5)), (D[3], "SPY", pytest.approx(103.0))]
    p = prices.load_prices(tmp_path)
    assert p.on("AAA", D[2]) == 49.0 and p.on("AAA", D[3]) == pytest.approx(52.0) and p.on("AAA", D[4]) is None
    assert all(start <= D[2] - dt.timedelta(days=10) for _, start in asked)
    assert prices.update_live(tmp_path, ["AAA", "SPY"], now, fetch) == []            # running again adds nothing
    later = prices.update_live(tmp_path, ["AAA", "SPY"], dt.datetime(2026, 11, 20, 23, 0, tzinfo=UTC), fetch)
    assert [(d, t) for d, t, _ in later] == [(D[4], "AAA"), (D[4], "SPY")]
    assert prices.load_prices(tmp_path).on("AAA", D[4]) == pytest.approx(60.0)
    assert (tmp_path / prices.LIVE).read_text().splitlines()[0] == "date,ticker,tr"


def test_live_update_refuses_a_symbol_whose_history_no_longer_matches(tmp_path):
    write_snapshot(tmp_path, {"SPY": [(D[0], 100.0), (D[1], 101.0), (D[2], 102.0)]})
    other = [(D[0], 10, 10.0), (D[1], 20, 20.0), (D[2], 15, 15.0), (D[3], 16, 16.0)]
    with pytest.raises(RuntimeError, match="disagrees"):
        prices.update_live(tmp_path, ["SPY"], dt.datetime(2026, 11, 20, 12, 0, tzinfo=UTC), lambda s, start: other)
    with pytest.raises(RuntimeError, match="does not reach back"):
        prices.update_live(tmp_path, ["SPY"], dt.datetime(2026, 11, 20, 12, 0, tzinfo=UTC), lambda s, start: other[3:])
    assert not (tmp_path / prices.LIVE).exists()


def test_live_update_builds_dollar_series_for_foreign_shares(tmp_path):
    write_snapshot(tmp_path, {"SPY": [(D[0], 100.0), (D[1], 101.0)], "SKH-USD": [(D[0], 800.0), (D[1], 820.0)]})
    won = [(D[0], 1_080_000, 1_080_000.0), (D[1], 1_107_000, 1_107_000.0), (D[2], 1_144_000, 1_144_000.0)]
    rate = [(D[0], 1350.0, 1350.0), (D[1], 1350.0, 1350.0), (D[2], 1300.0, 1300.0)]
    vendor = {"000660.KS": won, "KRW=X": rate}
    added = prices.update_live(tmp_path, ["SKH-USD"], dt.datetime(2026, 11, 19, 1, 0, tzinfo=UTC), lambda s, start: vendor[s])
    assert added == [(D[2], "SKH-USD", pytest.approx(820.0 * (1_144_000 / 1300.0) / (1_107_000 / 1350.0)))]
    # a few hours earlier the currency day had not closed, so nothing is final yet
    assert prices.final_through("SKH-USD", dt.datetime(2026, 11, 18, 22, 0, tzinfo=UTC)) == D[1]


def test_a_bar_far_from_the_one_before_it_is_refused_until_confirmed(tmp_path):
    write_snapshot(tmp_path, {"SPY": [(D[0], 100.0), (D[1], 101.0), (D[2], 102.0)], "AAA": [(D[0], 50.0), (D[1], 51.0), (D[2], 49.0)]})
    vendor = {"SPY": [(D[0], 100, 100.0), (D[1], 101, 101.0), (D[2], 102, 102.0), (D[3], 103, 103.0)],
              "AAA": [(D[0], 50, 50.0), (D[1], 51, 51.0), (D[2], 49, 49.0), (D[3], 490, 490.0)]}          # ten times yesterday: a bad print, or a split shown raw
    now = dt.datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    with pytest.raises(RuntimeError, match="--allow-jump AAA"):
        prices.update_live(tmp_path, ["AAA", "SPY"], now, lambda s, start: vendor[s])
    assert not (tmp_path / prices.LIVE).exists()                                                           # nothing stored, not even SPY
    vendor["AAA"][3] = (D[3], 20, 20.0)
    with pytest.raises(RuntimeError, match="0.41 times"):
        prices.update_live(tmp_path, ["AAA", "SPY"], now, lambda s, start: vendor[s])
    added = prices.update_live(tmp_path, ["AAA", "SPY"], now, lambda s, start: vendor[s], allow_jump={"AAA"})   # the owner says the fall is real
    assert (D[3], "AAA", pytest.approx(20.0)) in added
    bad_rate = {"000660.KS": [(D[0], 1000, 1000.0), (D[1], 1000, 1000.0)], "KRW=X": [(D[0], 1350.0, 1350.0), (D[1], 135.0, 135.0)]}
    write_snapshot(tmp_path / "fx", {"SPY": [(D[0], 100.0)], "SKH-USD": [(D[0], 0.74)]})
    with pytest.raises(RuntimeError, match="KRW=X: the rate"):
        prices.update_live(tmp_path / "fx", ["SKH-USD"], now, lambda s, start: bad_rate[s])


def test_an_adjustment_the_vendor_books_late_is_picked_up_by_the_next_bar(tmp_path):
    """AAA went ex-dividend on D[2]; the vendor's adjusted prices only showed it after that bar was stored."""
    write_snapshot(tmp_path, {"SPY": [(D[0], 100.0), (D[1], 101.0), (D[2], 102.0)], "AAA": [(D[0], 100.0), (D[1], 100.0), (D[2], 99.0)]})   # stored: a 1% fall
    late = [(D[0], 100, 99.0), (D[1], 100, 99.0), (D[2], 99, 99.0), (D[3], 99, 99.0)]       # now the days before the ex-date are adjusted down: no fall at all
    spy = [(D[0], 100, 100.0), (D[1], 101, 101.0), (D[2], 102, 102.0), (D[3], 103, 103.0)]
    before = prices.load_prices(tmp_path)
    added = prices.update_live(tmp_path, ["AAA", "SPY"], dt.datetime(2026, 11, 20, 12, 0, tzinfo=UTC), lambda s, start: {"AAA": late, "SPY": spy}[s])
    assert dict((t, v) for _, t, v in added)["AAA"] == pytest.approx(100.0)                 # flat since D[0] in total-return terms: the dividend is back
    after = prices.load_prices(tmp_path)
    assert [after.on("AAA", d) for d in D[:3]] == [before.on("AAA", d) for d in D[:3]]      # and no stored row moved
    big = lambda s, start: [(d, c, a * (0.8 if d < D[3] else 1)) for d, c, a in late] + [(D[4], 99, 99.0)]
    with pytest.raises(RuntimeError, match="disagrees"):                                     # a large disagreement is an error, not a dividend
        prices.update_live(tmp_path, ["AAA"], dt.datetime(2026, 11, 20, 23, 0, tzinfo=UTC), big)
    added = prices.update_live(tmp_path, ["AAA"], dt.datetime(2026, 11, 20, 23, 0, tzinfo=UTC), big, allow_jump={"AAA"})    # unless the owner sides with the vendor
    assert added == [(D[4], "AAA", pytest.approx(124.375))]                                  # the middle of what the four stored bars imply: 100, 123.75, 125, 125


def test_a_late_adjustment_is_caught_up_once_and_never_shows_again(tmp_path):
    """Run the update day after day across a dividend the vendor booked one day late: one catch-up, then the true series, with no echo
    when the under-stated bar later becomes the one the chain could have started from. One run a day and one run after many days agree."""
    days = weekdays(dt.date(2026, 11, 2), 16)
    true = [100.0 * 1.01 ** i for i in range(16)]                         # total return: +1% a day throughout
    ex = 6                                                                # the ex-dividend day: the price itself falls 2% net (1% gain less a 3% dividend)
    seen_early = [v if i < ex else v * 0.97 for i, v in enumerate(true)]  # what the vendor showed until the dividend was booked: prices, no adjustment

    def vendor(upto, booked):
        series = true if booked else seen_early                          # once booked, earlier days are scaled so that ratios are total returns
        return lambda s, start: [(d, 0, series[i]) for i, d in enumerate(days) if i <= upto and d >= start]

    def run(folder, upto, booked):
        now = dt.datetime.combine(days[upto] + dt.timedelta(days=1), dt.time(1, 7), UTC)
        return prices.update_live(folder, ["AAA", "SPY"], now, lambda s, start: vendor(upto, booked)(s, start) if s == "AAA" else [(d, 0, 1.0) for d in days[:upto + 1] if d >= start])

    daily = tmp_path / "daily"; write_snapshot(daily, {"SPY": [(d, 1.0) for d in days[:6]], "AAA": [(d, v) for d, v in zip(days[:6], true)]})
    run(daily, ex, booked=False)                                          # the ex-date bar is stored before the vendor books the dividend
    assert prices.load_prices(daily).on("AAA", days[ex]) == pytest.approx(true[ex] * 0.97)
    for upto in range(ex + 1, 16):
        run(daily, upto, booked=True)
    got = prices.load_prices(daily)
    assert got.on("AAA", days[ex]) == pytest.approx(true[ex] * 0.97)      # the stored bar is never rewritten
    for i in range(ex + 1, 16):
        assert got.on("AAA", days[i]) == pytest.approx(true[i], rel=1e-6), days[i]      # caught up on the next bar and right ever after

    late = tmp_path / "late"; write_snapshot(late, {"SPY": [(d, 1.0) for d in days[:6]], "AAA": [(d, v) for d, v in zip(days[:6], true)]})
    run(late, ex, booked=False); run(late, 15, booked=True)               # the same history in two runs instead of ten
    other = prices.load_prices(late)
    assert all(other.on("AAA", d) == pytest.approx(got.on("AAA", d), rel=1e-6) for d in days)


def test_a_first_time_ticker_keeps_only_final_unpublished_bars_and_an_optional_one_may_fail(tmp_path):
    write_snapshot(tmp_path, {"SPY": [(D[0], 100.0), (D[1], 101.0), (D[2], 102.0)]})
    vendor = {"SPY": [(D[0], 100, 100.0), (D[1], 101, 101.0), (D[2], 102, 102.0), (D[3], 103, 103.0), (D[4], 104, 104.0)],
              "NEW": [(D[0], 5, 5.0), (D[1], 50, 50.0), (D[2], 51, 51.0), (D[3], 52, 52.0), (D[4], 53, 53.0)]}      # its history has a tenfold day before we ever held it

    def fetch(symbol, start):
        if symbol == "GONE":
            raise OSError("no such symbol")
        return vendor[symbol]
    now = dt.datetime(2026, 11, 20, 12, 0, tzinfo=UTC)                                      # Friday midday: Thursday is final, Friday is not
    with pytest.raises(OSError):
        prices.update_live(tmp_path, ["GONE", "SPY"], now, fetch)
    added = prices.update_live(tmp_path, ["GONE", "NEW", "SPY"], now, fetch, published_through=D[2], optional={"GONE"})
    assert added == [(D[3], "NEW", 52.0), (D[3], "SPY", pytest.approx(103.0))]              # nothing on or before the published day, nothing from today


def test_conflicting_duplicate_price_rows_are_an_error(tmp_path):
    write_snapshot(tmp_path, {"SPY": [(D[0], 100.0), (D[1], 101.0)]})
    (tmp_path / prices.LIVE).write_text(f"date,ticker,tr\n{D[1]},SPY,101\n{D[2]},SPY,102\n")
    assert prices.load_prices(tmp_path).sessions() == D[:3]                                 # the same value twice is harmless
    (tmp_path / prices.LIVE).write_text(f"date,ticker,tr\n{D[1]},SPY,105\n")
    with pytest.raises(ValueError, match="stored twice with different values"):
        prices.load_prices(tmp_path)
    (tmp_path / prices.LIVE).write_text(f"date,ticker,tr\n{D[2]},SPY,0\n")
    with pytest.raises(ValueError, match="non-positive"):
        prices.load_prices(tmp_path)


def test_vendor_parsers(monkeypatch):
    """The two download routes read what Yahoo returns: local exchange dates, close and adjusted close, no empty rows."""
    import pandas as pd
    import requests
    import yfinance

    idx = pd.DatetimeIndex(["2026-11-17", "2026-11-18", "2026-11-19"], tz="America/New_York")
    frame = pd.DataFrame({"Open": [1.0, 1.0, 1.0], "High": [1.0, 1.0, 1.0], "Low": [1.0, 1.0, 1.0], "Close": [100.0, 102.0, float("nan")],
                          "Adj Close": [99.0, 101.0, float("nan")], "Volume": [1, 1, 0], "Dividends": [0.0, 0.0, 0.0], "Stock Splits": [0.0, 0.0, 0.0]}, index=idx)

    class Ticker:
        def __init__(self, symbol): self.symbol = symbol
        def history(self, **kw):
            assert kw == {"start": "2026-11-05", "interval": "1d", "auto_adjust": False}
            return frame
    monkeypatch.setattr(yfinance, "Ticker", Ticker)
    assert prices._from_yfinance("SPY", dt.date(2026, 11, 5)) == [(dt.date(2026, 11, 17), 100.0, 99.0), (dt.date(2026, 11, 18), 102.0, 101.0)]

    seoul = 9 * 3600; stamps = [int(dt.datetime(2026, 11, 17, 0, 0, tzinfo=UTC).timestamp()), int(dt.datetime(2026, 11, 18, 0, 0, tzinfo=UTC).timestamp())]   # 09:00 in Seoul
    body = {"chart": {"result": [{"meta": {"gmtoffset": seoul}, "timestamp": stamps,
                                  "indicators": {"quote": [{"close": [1_098_000.0, None]}], "adjclose": [{"adjclose": [1_098_000.0, None]}]}}]}}

    class Reply:
        def raise_for_status(self): pass
        def json(self): return body
    monkeypatch.setattr(requests, "get", lambda url, **kw: Reply())
    assert prices._from_chart_api("000660.KS", dt.date(2026, 11, 5)) == [(dt.date(2026, 11, 17), 1_098_000.0, 1_098_000.0)]

    calls_made = []

    def broken(symbol, start):
        calls_made.append(symbol); raise OSError("blocked")
    monkeypatch.setattr(prices, "_from_yfinance", broken); monkeypatch.setattr(prices, "_from_chart_api", broken)
    with pytest.raises(RuntimeError, match="no price history for SPY"):
        prices.yahoo_history("SPY", dt.date(2026, 11, 5), attempts=2, pause=0)
    assert len(calls_made) == 4


def test_final_bar_times():
    f = prices.final_through
    assert f("SPY", dt.datetime(2026, 11, 18, 21, 5, tzinfo=UTC)) == dt.date(2026, 11, 17)       # minutes after the winter close: not final yet
    assert f("SPY", dt.datetime(2026, 11, 18, 22, 29, tzinfo=UTC)) == dt.date(2026, 11, 17)
    assert f("SPY", dt.datetime(2026, 11, 18, 22, 30, tzinfo=UTC)) == dt.date(2026, 11, 18)
    assert f("SPY", dt.datetime(2026, 7, 15, 21, 0, tzinfo=UTC)) == dt.date(2026, 7, 14)         # summer: an hour after the close is still too early
    assert f("BTC-USD", dt.datetime(2026, 11, 18, 23, 59, tzinfo=UTC)) == dt.date(2026, 11, 17)
    assert f("BTC-USD", dt.datetime(2026, 11, 19, 0, 29, tzinfo=UTC)) == dt.date(2026, 11, 17)
    assert f("BTC-USD", dt.datetime(2026, 11, 19, 0, 30, tzinfo=UTC)) == dt.date(2026, 11, 18)
    assert f("000660.KS", dt.datetime(2026, 11, 18, 6, 45, tzinfo=UTC)) == dt.date(2026, 11, 17)
    assert f("000660.KS", dt.datetime(2026, 11, 18, 7, 30, tzinfo=UTC)) == dt.date(2026, 11, 18)
    for run in (dt.datetime(2026, 11, 19, 1, 7, tzinfo=UTC), dt.datetime(2026, 7, 16, 1, 7, tzinfo=UTC)):   # the scheduled slot, winter and summer
        day = run.date() - dt.timedelta(days=1)
        assert {f(t, run) for t in ("SPY", "BTC-USD", "KRW=X", "000660.KS", "SKH-USD")} == {day}


def test_days_are_published_only_once_every_held_price_is_final(calls, store):
    plan = engine.plan(calls, store.sessions())
    last = store.sessions()[-1]
    assert cli.publishable_through(plan, store, dt.datetime(2026, 11, 19, 7, 0, tzinfo=UTC)) == last
    assert cli.publishable_through(plan, store, dt.datetime(2026, 11, 18, 21, 0, tzinfo=UTC)) < last   # an hour before the bar is final


# ---------- the published contract ----------
def test_build_and_publish(tmp_path, calls, store, cfg):
    now = dt.datetime(2026, 11, 19, 7, 0, tzinfo=UTC)
    built = cli.build(calls, store, cfg, now)
    table = built["daily"]["allin_12m"]
    contract.validate_daily(table)
    assert table["date"].min() == cfg["backtest_start"] and table["pnl"][0] == 0.0
    assert table.filter(pl.col("mode") == "backtest")["date"].max() < cfg["paper_start"]
    assert 10_000 + table.filter(pl.col("date") <= MARK)["pnl"].sum() == pytest.approx(24_634.14, abs=0.5)
    summary = built["summary"]
    assert set(summary["strategy_id"]) == {"allin_12m", "allin_12m_hosts", "allin_12m_guests", "spy_buy_hold"}
    assert summary.filter(pl.col("paper_traded"))["role"].to_list() == ["monitor"]
    with_bills = cli.build(calls, store, cfg, now, cli.load_bills(ROOT / "data"))["summary"]
    both = dict(zip(with_bills["strategy_id"], with_bills["sharpe_vs_bills"]))
    assert both["allin_12m"] < both["spy_buy_hold"] < 0.7 and summary["sharpe_vs_bills"].null_count() == 4
    out = str(tmp_path / "results")
    cli.publish(out, cfg, built, store, len(calls), now_utc=now)
    index = json.loads((tmp_path / "results" / "index.json").read_text())
    assert index["contract_version"] == 1 and index["strategies"] == ["allin_12m"] and index["data_through"] == str(store.sessions()[-1])
    manifest = json.loads((tmp_path / "results" / "allin_12m" / "manifest.json").read_text())
    contract.validate_manifest(manifest)
    assert manifest["role"] == "monitor" and manifest["days_per_year"] == 252
    held = list(csv.DictReader((tmp_path / "results" / "allin_12m" / "positions.csv").open()))
    assert sum(float(r["weight_pct"]) for r in held) == pytest.approx(100, abs=0.2)
    cli.publish(out, cfg, built, store, len(calls), now_utc=now)                     # publishing the same thing again is fine
    changed = dict(built, daily={"allin_12m": table.with_columns(pl.when(pl.col("date") == MARK).then(pl.col("pnl") + 5).otherwise(pl.col("pnl")).alias("pnl"))})
    with pytest.raises(RuntimeError, match="would change"):
        cli.publish(out, cfg, changed, store, len(calls), now_utc=now)
    shorter = dict(built, daily={"allin_12m": table.head(table.height - 1)})
    with pytest.raises(RuntimeError, match="would disappear"):
        cli.publish(out, cfg, shorter, store, len(calls), now_utc=now)


def test_paper_rows_follow_the_backtest_rows(calls, store, cfg):
    early = dict(cfg, paper_start=dt.date(2026, 11, 9))
    built = cli.build(calls, store, early, dt.datetime(2026, 11, 19, 7, 0, tzinfo=UTC))
    table = built["daily"]["allin_12m"]
    paper = table.filter(pl.col("mode") == "paper")
    assert paper["date"].min() == dt.date(2026, 11, 9) and paper["pnl"][0] == 0.0 and paper["capital"][0] == 10_000.0
    assert table.filter(pl.col("mode") == "backtest")["date"].max() == dt.date(2026, 11, 6)
    assert table["date"].is_sorted() and not table["date"].is_duplicated().any()
    book, res = built["books"]["allin_12m"]
    assert book == "paper" and 10_000 + paper["pnl"].sum() == pytest.approx(res.equity[-1])


def test_first_production_days(tmp_path, calls, store, cfg):
    """A dry run of the scheduled job over the first two paper days, with a stand-in for the data vendor."""
    data = tmp_path / "data"; data.mkdir()
    shutil.copy(ROOT / "data" / prices.SNAPSHOT, data / prices.SNAPSHOT)
    new_days = [dt.date(2026, 11, 19), dt.date(2026, 11, 20)]
    local = {"000660.KS": "SKH-USD", "005930.KS": "SAMSUNG-USD"}

    def fetch(symbol, start):
        if symbol == "KRW=X":
            return [(d, 1350.0, 1350.0) for d in weekdays(start, 40) if d <= new_days[-1]]
        base, k = local.get(symbol, symbol), (1350.0 if symbol in local else 1.0)
        bars = [(d, store.on(base, d) * k, store.on(base, d) * k) for d in store._dates[base] if d >= start]
        level = bars[-1][1]
        for d in new_days:
            level *= 1.01; bars.append((d, level, level))            # every ticker gains 1% a day
        return bars
    now = dt.datetime(2026, 11, 21, 1, 7, tzinfo=UTC)                # the Saturday slot after Friday's session
    need, optional = cli.tickers_to_update(engine.plan(calls, store.sessions()), store, "SPY")
    assert {"SPY", "SKH-USD", "SMH", "XLU"} <= set(need) and "BTC-USD" not in need and not optional      # only what is still held
    added = prices.update_live(data, need, now, fetch)
    assert {d for d, _, _ in added} == set(new_days) and len(added) == 2 * len(need)
    fresh = prices.load_prices(data)
    built = cli.build(calls, fresh, cfg, now)
    table = built["daily"]["allin_12m"]
    paper = table.filter(pl.col("mode") == "paper")
    assert paper["date"].to_list() == new_days
    assert paper["pnl"][0] == 0.0 and paper["capital"][0] == 10_000.0 and paper["n_trades"][0] == 19
    assert paper["pnl"][1] == pytest.approx(100.0, abs=1e-3)         # 19 long calls, each up 1% (stored levels keep 8 digits)
    assert table.filter(pl.col("mode") == "backtest")["date"].max() == dt.date(2026, 11, 18)
    out = str(tmp_path / "results")
    cli.publish(out, cfg, built, fresh, len(calls), now_utc=now)
    held = list(csv.DictReader((tmp_path / "results" / "allin_12m" / "positions.csv").open()))
    assert len(held) == 19 and {r["book"] for r in held} == {"paper"} and {r["bought"] for r in held} == {"2026-11-19"}
    assert {r["leaves_on_or_after"] for r in held if r["call_id"] == "30"} == {"2027-01-12"}


def test_a_late_foreign_bar_never_changes_a_published_day(tmp_path, calls, store, cfg):
    """The vendor posts Friday's Seoul bars a run late. Friday is published with Thursday's Seoul close and stays that way."""
    data = tmp_path / "data"; data.mkdir(); shutil.copy(ROOT / "data" / prices.SNAPSHOT, data / prices.SNAPSHOT)
    out = str(tmp_path / "results")
    thu, fri, mon = dt.date(2026, 11, 19), dt.date(2026, 11, 20), dt.date(2026, 11, 23)
    local = {"000660.KS": "SKH-USD", "005930.KS": "SAMSUNG-USD"}
    moves = {thu: 1.00, fri: 1.02, mon: 1.00}; seoul = {thu: 1.00, fri: 1.05, mon: 0.99}

    def vendor(known_days, seoul_known):
        def fetch(symbol, start):
            if symbol == "KRW=X":
                return [(d, 1350.0, 1350.0) for d in weekdays(start, 60) if d <= max(known_days)]
            base, k = local.get(symbol, symbol), (1350.0 if symbol in local else 1.0)
            bars = [(d, store.on(base, d) * k, store.on(base, d) * k) for d in store._dates[base] if d >= start]
            level = bars[-1][1]
            for d in (thu, fri, mon):
                level *= (seoul if symbol in local else moves)[d]
                if d in (seoul_known if symbol in local else known_days):
                    bars.append((d, level, level))
            return bars
        return fetch

    def job(now, known_days, seoul_known):
        current = prices.load_prices(data)
        need, _ = cli.tickers_to_update(engine.plan(calls, current.sessions()), current, "SPY")
        added = prices.update_live(data, need, now, vendor(known_days, seoul_known), published_through=cli.last_published(out))
        current = prices.load_prices(data)
        built = cli.build(calls, current, cfg, now)
        cli.publish(out, cfg, built, current, len(calls), now_utc=now)
        return added, built["daily"]["allin_12m"].filter(pl.col("mode") == "paper")

    job(dt.datetime(2026, 11, 20, 1, 7, tzinfo=UTC), {thu}, {thu})
    added, paper = job(dt.datetime(2026, 11, 21, 1, 7, tzinfo=UTC), {thu, fri}, {thu})          # Friday's Seoul bars are not in the vendor's history yet
    assert paper["date"].to_list() == [thu, fri] and not [r for r in added if r[1] in local.values()]
    friday_pnl = paper["pnl"][1]
    added, paper = job(dt.datetime(2026, 11, 21, 7, 7, tzinfo=UTC), {thu, fri}, {thu, fri})     # now they are: too late for Friday
    assert added == [] and paper["pnl"][1] == friday_pnl
    assert prices.load_prices(data).on("SKH-USD", fri) is None
    added, paper = job(dt.datetime(2026, 11, 24, 1, 7, tzinfo=UTC), {thu, fri, mon}, {thu, fri, mon})
    assert paper["date"].to_list() == [thu, fri, mon] and paper["pnl"][1] == friday_pnl
    after = prices.load_prices(data)
    assert after.on("SKH-USD", mon) == pytest.approx(after.on("SKH-USD", thu) * 1.05 * 0.99)   # the level is right again; Friday's move lands on Monday


def test_a_call_cannot_be_slipped_onto_a_published_day(tmp_path, calls, store, cfg):
    now = dt.datetime(2026, 11, 19, 7, 0, tzinfo=UTC); out = str(tmp_path / "results")
    built = cli.build(calls, store, cfg, now)
    cli.publish(out, cfg, built, store, len(calls), now_utc=now)
    late = make_call(published="2026-11-16T15:00:00Z", recorded="2026-11-17T20:00:00Z", legs="SPY:1")    # typed in afterwards: enters at the last published close
    assert engine.plan([late], store.sessions())[0].entry == FROZEN_THROUGH
    changed = cli.build([*calls, late], store, cfg, now)
    contract.check_append_only(built["daily"]["allin_12m"], changed["daily"]["allin_12m"])              # that day's PnL is untouched, so this alone sees nothing
    with pytest.raises(RuntimeError, match="trades or capital"):
        cli.publish(out, cfg, changed, store, len(calls) + 1, now_utc=now)
    cli.publish(out, cfg, changed, store, len(calls) + 1, now_utc=now, allow_restate=True)


def test_a_day_waits_for_the_final_bar_of_a_crypto_leg():
    days, st = small_store(4, BTC_USD=[1, 2, 3, 4])
    plan = engine.plan([make_call(published="2026-01-03T15:00:00Z", legs="SPY:0.5;BTC-USD:0.5")], days)
    last = days[-1]                                                                          # Thu 8 Jan 2026
    after_us_close = dt.datetime.combine(last, dt.time(22, 45), UTC)
    assert cli.publishable_through(plan, st, after_us_close) == days[-2]                     # the US bar is final, the crypto day is not over
    assert cli.publishable_through(plan, st, dt.datetime.combine(last + dt.timedelta(days=1), dt.time(1, 7), UTC)) == last
    us_only = engine.plan([make_call(published="2026-01-03T15:00:00Z", legs="SPY:1")], days)
    assert cli.publishable_through(us_only, st, after_us_close) == last


def test_the_contract_refuses_unknown_modes_and_keeps_strategies_with_a_paper_record(tmp_path):
    row = {"date": [dt.date(2026, 11, 19)], "mode": ["paper"], "pnl": [1.0], "capital": [10.0], "n_trades": [0]}
    with pytest.raises(ValueError, match="unknown mode"):
        contract.validate_daily(pl.DataFrame(dict(row, mode=["simulated"]), schema=contract.DAILY_SCHEMA))
    out = str(tmp_path / "r")
    manifest = {"id": "old", "name": "Old", "market": "m", "currency": "USD", "days_per_year": 252, "backtest_start": "2021-01-01", "paper_start": "2026-11-19", "description": ""}
    contract.publish_strategy(out, manifest, pl.DataFrame(row, schema=contract.DAILY_SCHEMA))
    contract.publish_index(out, "s", ["old"], pl.DataFrame({"strategy_id": ["old"]}), "2026-11-20T00:00:00Z", "2026-11-19")
    with pytest.raises(RuntimeError, match="has paper or live days"):
        contract.prune_removed(out, [])
    assert contract.prune_removed(out, ["old"]) == []
    assert contract.prune_removed(out, [], allow_restate=True) == ["old"] and not (tmp_path / "r" / "old").exists()


def test_carried_prices_are_reported(calls, store):
    res = engine.simulate(engine.plan(calls, store.sessions()), store, 10_000.0, end=dt.date(2026, 10, 9))    # Seoul shut from 24 Sep to 30 Sep is over; nothing stale
    assert cli.stale_legs(res, store) == []
    res = engine.simulate(engine.plan(calls, store.sessions()), store, 10_000.0, end=dt.date(2026, 9, 25))    # Friday: last Seoul close was Wednesday 23 Sep
    assert cli.stale_legs(res, store) == []
    days, st = small_store(8, SKH_USD=[7, 7, None, None, None, None, None, None])
    res = engine.simulate(engine.plan([make_call(call_id=7, published="2026-01-03T15:00:00Z", legs="SPY:0.5;SKH-USD:0.5")], days), st, 10_000.0)
    assert cli.stale_legs(res, st) == ["call 7: SKH-USD has no price since 2026-01-06; it is carried at that close"]


def test_command_line_run(tmp_path):
    assert cli.main(["run", "--out", str(tmp_path / "out")]) == 0
    assert (tmp_path / "out" / "allin_12m" / "daily.parquet").exists()
    assert pl.read_parquet(tmp_path / "out" / "backtest_summary.parquet").height == 4


def test_committed_results_match_the_code(ledger_all, cfg):
    """The results folder in the repository is what this code produces from the committed ledger and prices."""
    path = ROOT / "results" / "allin_12m" / "daily.parquet"
    if not path.exists():
        pytest.skip("no results committed yet")
    old = pl.read_parquet(path)
    index = json.loads((ROOT / "results" / "index.json").read_text())
    now = dt.datetime.fromisoformat(index["updated_utc"].replace("Z", "+00:00"))
    traded = [c for c in ledger_all if not cli.is_pending(c, cfg)]
    new = cli.build(traded, prices.load_prices(ROOT / "data"), cfg, now)["daily"]["allin_12m"]
    contract.check_append_only(old, new)
    assert new.height >= old.height
