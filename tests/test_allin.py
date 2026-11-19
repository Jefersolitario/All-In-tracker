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
    """Run the real pipeline twice, once with an unstamped line added: the published days are identical."""
    new_id = max(c.call_id for c in ledger_all) + 1
    path = tmp_path / "calls.csv"
    path.write_text((ROOT / "calls.csv").read_text(encoding="utf-8") + f"{new_id},guid-a,2026-11-18T22:00:00Z,Friedberg,Uranium,URA:1,active,,,,\n", encoding="utf-8")
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
    need = cli.tickers_to_update(engine.plan(calls, store.sessions()), store, "SPY")
    assert {"SPY", "SKH-USD", "SMH", "XLU"} <= set(need) and "BTC-USD" not in need      # only what is still held
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
