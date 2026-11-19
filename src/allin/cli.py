"""Command line: build the backtest, advance the paper book and publish the results contract.

    python -m allin.cli run                 recompute from stored prices, write ./results
    python -m allin.cli run --fetch         first append the latest final daily prices
    python -m allin.cli run --out gs://bucket/all-in-tracker      publish somewhere else (needs gcsfs)
    python -m allin.cli status              print the book as it stands: what is held, since when, until when
    python -m allin.cli stamp               give new calls the time they reached the ledger (the scheduled job does this)
    python -m allin.cli episodes            append newly published episodes to data/episodes.csv from the publisher's feed
"""
from __future__ import annotations

import argparse
import bisect
import csv
import datetime as dt
import hashlib
import io
import math
import pathlib
import tomllib

import polars as pl

from . import contract, engine, episodes, prices
from .ledger import Call, load_calls, plus_year

ROOT = pathlib.Path(__file__).resolve().parents[2]
DAYS_PER_YEAR = 252
FILTERS = {"all": lambda c: True, "hosts": lambda c: c.host_only, "guests": lambda c: not c.host_only}


def load_config(path: pathlib.Path) -> dict:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    cfg["backtest_start"] = dt.date.fromisoformat(cfg["backtest_start"])
    cfg["paper_start"] = dt.date.fromisoformat(cfg["paper_start"])
    cfg["start_cash"] = float(cfg["start_cash"])
    cfg["recovered_calls_through"] = int(cfg.get("recovered_calls_through", 0))
    ids = [s["id"] for s in cfg["strategy"]]
    if len(set(ids)) != len(ids):
        raise ValueError("strategy ids must be unique")
    for s in cfg["strategy"]:
        if s.get("calls", "all") not in FILTERS:
            raise ValueError(f"{s['id']}: calls must be one of {sorted(FILTERS)}")
        s["role"] = s.get("role", "portfolio") if s.get("paper", False) else "tested"
        if s["role"] not in ("portfolio", "monitor", "tested"):
            raise ValueError(f"{s['id']}: role must be 'portfolio' or 'monitor'")
    return cfg


def fingerprint(files: list[pathlib.Path]) -> str:
    """Moves only when the named files change, and does not need git history (CI checks out a single commit)."""
    h = hashlib.sha1()
    for f in sorted(files):
        h.update(f.name.encode() + b"\0" + f.read_bytes())
    return h.hexdigest()[:10]


def is_pending(call: Call, cfg: dict) -> bool:
    """A call added after the recovered backtest that has no stamp yet: it is on file but not traded."""
    return call.status == "active" and call.call_id > cfg["recovered_calls_through"] and call.recorded_utc is None


def check_ledger(calls: list[Call], cfg: dict, now_utc: dt.datetime) -> None:
    """Refuse a ledger whose timestamps cannot be right."""
    for c in calls:
        if c.published_utc > now_utc:
            raise ValueError(f"call {c.call_id}: published_utc is in the future")
        if c.recorded_utc is not None and c.recorded_utc > now_utc:
            raise ValueError(f"call {c.call_id}: recorded_utc is in the future")
        if c.recorded_utc is not None and c.recorded_utc < c.published_utc:
            raise ValueError(f"call {c.call_id}: recorded before the episode was published")


def stamp_calls(path: str | pathlib.Path, cfg: dict, now_utc: dt.datetime) -> list[int]:
    """Write the current time into `recorded_utc` of every new call that has none. Returns the ids stamped.

    A call is bought at the close of the first session after its stamp, so the stamp has to be the moment the line
    became part of the live ledger. The scheduled job runs this on `main`; nothing else should fill the field in.
    Every other byte of the file is left as it was.
    """
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    header = rows[0]
    i_id, i_status, i_rec = header.index("call_id"), header.index("status"), header.index("recorded_utc")
    done = []
    for r in rows[1:]:
        if int(r[i_id]) > cfg["recovered_calls_through"] and r[i_status] == "active" and not r[i_rec].strip():
            r[i_rec] = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"); done.append(int(r[i_id]))
    if done:
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f, lineterminator="\n").writerows(rows)
    return done


def tickers_to_update(positions: list[engine.Position], store: prices.Prices, benchmark: str) -> list[str]:
    """Benchmark, calendar, and the legs of every call that is still open or has not entered yet."""
    last = store.sessions()[-1]
    need = {benchmark, prices.CALENDAR}
    for p in positions:
        if p.exit is None or p.exit >= last:
            need.update(t for t, _ in p.call.legs)
    return sorted(need)


def publishable_through(positions: list[engine.Position], store: prices.Prices, now_utc: dt.datetime) -> dt.date:
    """Last session that can be published for good: every leg held that day has its final price for that day."""
    done = engine.complete_through(positions, store)
    last = store.sessions()[-1]
    live = {t for p in positions if p.entry is not None and (p.exit is None or p.exit >= last) for t, _ in p.call.legs}
    final = min([prices.final_through(t, now_utc) for t in live | {prices.CALENDAR}])
    return max(d for d in store.sessions() if d <= min(done, final))


def daily_table(res: engine.Result, mode: str) -> pl.DataFrame:
    pnl = [0.0] + [res.equity[i] - res.equity[i - 1] for i in range(1, len(res.equity))]
    capital = [e if n else 0.0 for e, n in zip(res.equity, res.n_active)]
    return pl.DataFrame({"date": res.dates, "mode": [mode] * len(res.dates), "pnl": pnl, "capital": capital, "n_trades": res.n_trades},
                        schema=contract.DAILY_SCHEMA)


def load_bills(data_dir: str | pathlib.Path) -> list[tuple[dt.date, float]]:
    """Three-month Treasury bill yield in percent a year (FRED series DTB3); empty when the file is absent."""
    path = pathlib.Path(data_dir) / "tbill_3m.csv"
    if not path.exists():
        return []
    with open(path, newline="") as f:
        return sorted((dt.date.fromisoformat(r["date"]), float(r["yield_pct"])) for r in csv.DictReader(f) if r["yield_pct"] not in ("", "."))


def bill_returns(bills: list[tuple[dt.date, float]], dates: list[dt.date]) -> list[float] | None:
    """Daily risk-free return for each day after the first, from the latest yield on or before it."""
    if not bills:
        return None
    days = [d for d, _ in bills]
    return [bills[max(bisect.bisect_right(days, d) - 1, 0)][1] / 100 / DAYS_PER_YEAR for d in dates[1:]]


def market_fit(equity: list[float], bench: list[float]) -> dict:
    """Daily returns regressed on the benchmark's: beta, and what is left over (alpha) with its t statistic."""
    y = [equity[i] / equity[i - 1] - 1 for i in range(1, len(equity))]
    x = [bench[i] / bench[i - 1] - 1 for i in range(1, len(bench))]
    n = len(y)
    if n < 30:
        return {"beta_spy": None, "alpha_pct_yr": None, "alpha_t": None}
    mx, my = sum(x) / n, sum(y) / n
    sxx = sum((a - mx) ** 2 for a in x)
    beta = sum((a - mx) * (b - my) for a, b in zip(x, y)) / sxx
    alpha = my - beta * mx
    s2 = sum((b - alpha - beta * a) ** 2 for a, b in zip(x, y)) / (n - 2)
    se = math.sqrt(s2 * (1 / n + mx * mx / sxx))
    return {"beta_spy": beta, "alpha_pct_yr": alpha * DAYS_PER_YEAR * 100, "alpha_t": alpha / se if se > 0 else None}


def summary_row(sid: str, name: str, role: str, paper: bool, res: engine.Result, bench: list[float], start_cash: float, store: prices.Prices,
                bills: list[tuple[dt.date, float]]) -> dict:
    s = engine.stats(res.equity, DAYS_PER_YEAR, bill_returns(bills, res.dates))
    end = res.dates[-1]
    entered = [p for p in res.positions.values() if p.entry <= end]
    closed = [p for p in entered if p.exit is not None and p.exit <= end]
    wins = sum(1 for p in closed if engine.call_value(p.call, p.entry, p.exit, store) > 1)
    return {"strategy_id": sid, "name": name, "paper_traded": paper, "role": role, "start": res.dates[0], "end": end, "buys": len(entered), "put_in": start_cash,
            "pnl": s["pnl"], "return_pct": s["return_pct"], "sharpe": s["sharpe"], "sortino": s["sortino"], "max_dd": s["max_dd"],
            "sharpe_per_dollar": s["sharpe_per_dollar"], "placebo_p": None, "sharpe_vs_bills": s.get("sharpe_vs_bills"), "max_dd_pct": s["max_dd_pct"],
            "closed": len(closed), "wins": wins, **market_fit(res.equity, bench)}


def positions_csv(res: engine.Result, book: str, store: prices.Prices) -> bytes:
    """The book at the last published close: one line per call held."""
    end = res.dates[-1]; total = sum(res.holdings.values())
    buf = io.StringIO(); w = csv.writer(buf, lineterminator="\n")
    w.writerow(("as_of", "book", "call_id", "speaker", "call", "legs", "episode_published_utc", "bought", "leaves_on_or_after", "value_usd", "weight_pct", "return_since_bought_pct"))
    for k, usd in sorted(res.holdings.items()):
        p = res.positions[k]
        leaves = p.exit or (min(plus_year(p.rule_entry), p.call.forced_exit) if p.call.forced_exit else plus_year(p.rule_entry))
        w.writerow((end, book, k, p.call.speaker, p.call.call, ";".join(f"{t}:{x:g}" for t, x in p.call.legs), p.call.published_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    p.entry, leaves, f"{usd:.2f}", f"{100 * usd / total:.2f}", f"{100 * (engine.call_value(p.call, p.entry, end, store) - 1):.2f}"))
    return buf.getvalue().encode()


def build(calls: list[Call], store: prices.Prices, cfg: dict, now_utc: dt.datetime, bills: list[tuple[dt.date, float]] | None = None) -> dict:
    """Everything that gets published, computed from the ledger and the stored prices alone."""
    bills = bills or []
    sessions = store.sessions()
    through = publishable_through(engine.plan(calls, sessions), store, now_utc)
    bt_end = max(d for d in sessions if d < cfg["paper_start"] and d <= through)
    bench_t = cfg.get("benchmark", "SPY")
    daily, rows, books = {}, [], {}
    for s in cfg["strategy"]:
        mine = [c for c in calls if FILTERS[s.get("calls", "all")](c)]
        bt = engine.simulate(engine.plan(mine, sessions), store, cfg["start_cash"], end=bt_end)
        keep = [i for i, d in enumerate(bt.dates) if d >= cfg["backtest_start"]]
        if keep and keep[0] > 0:
            raise ValueError(f"{s['id']}: a call enters before backtest_start")
        bench = [store.on(bench_t, d) for d in bt.dates]
        rows.append(summary_row(s["id"], s["name"], s["role"], bool(s.get("paper", False)), bt, bench, cfg["start_cash"], store, bills))
        if not s.get("paper", False):
            continue
        table = daily_table(bt, "backtest"); book = ("backtest", bt)
        plan = engine.plan(mine, sessions, not_before=cfg["paper_start"])
        if any(p.entry is not None and p.entry <= through for p in plan):
            paper = engine.simulate(plan, store, cfg["start_cash"], end=through)       # the paper book starts in cash on paper_start
            table = pl.concat([table, daily_table(paper, "paper")]); book = ("paper", paper)
        daily[s["id"]] = table; books[s["id"]] = book
    first = min(r["start"] for r in rows); days = [d for d in sessions if first <= d <= bt_end]
    level = [cfg["start_cash"] * store.on(bench_t, d) / store.on(bench_t, days[0]) for d in days]
    ref_stats = engine.stats(level, DAYS_PER_YEAR, bill_returns(bills, days))
    rows.append({"strategy_id": f"{bench_t.lower()}_buy_hold", "name": f"{bench_t} bought on {days[0]} and held, dividends reinvested", "paper_traded": False, "role": "tested",
                 "start": days[0], "end": bt_end, "buys": 1, "put_in": cfg["start_cash"], "pnl": ref_stats["pnl"], "return_pct": ref_stats["return_pct"],
                 "sharpe": ref_stats["sharpe"], "sortino": ref_stats["sortino"], "max_dd": ref_stats["max_dd"], "sharpe_per_dollar": ref_stats["sharpe_per_dollar"],
                 "placebo_p": None, "sharpe_vs_bills": ref_stats.get("sharpe_vs_bills"), "max_dd_pct": ref_stats["max_dd_pct"], "closed": 0, "wins": 0,
                 "beta_spy": 1.0, "alpha_pct_yr": 0.0, "alpha_t": None})
    summary = pl.DataFrame(rows, schema_overrides={"placebo_p": pl.Float64, "sharpe_vs_bills": pl.Float64, "alpha_t": pl.Float64, "beta_spy": pl.Float64,
                                                   "alpha_pct_yr": pl.Float64, "put_in": pl.Float64})
    return {"daily": daily, "summary": summary, "books": books, "through": through}


def publish(out: str, cfg: dict, built: dict, store: prices.Prices, n_calls: int, allow_restate: bool = False, now_utc: dt.datetime | None = None,
            calls_path: str | pathlib.Path | None = None, config_path: str | pathlib.Path | None = None) -> None:
    now_utc = now_utc or dt.datetime.now(dt.timezone.utc)
    code = fingerprint([*ROOT.glob("src/allin/*.py"), pathlib.Path(config_path or ROOT / "strategies.toml")])
    ledger = fingerprint([pathlib.Path(calls_path or ROOT / "calls.csv")])
    for s in cfg["strategy"]:
        if not s.get("paper", False):
            continue
        manifest = {"id": s["id"], "name": s["name"], "market": "US-listed stocks and ETFs, with some crypto and foreign shares", "currency": "USD",
                    "days_per_year": DAYS_PER_YEAR, "backtest_start": cfg["backtest_start"], "paper_start": cfg["paper_start"], "description": s.get("description", ""),
                    "start_cash_usd": cfg["start_cash"], "code_version": code, "ledger_version": ledger, "calls_in_ledger": n_calls, "source_repo": cfg["source"], "role": s["role"]}
        contract.publish_strategy(out, manifest, built["daily"][s["id"]], allow_restate=allow_restate)
        book, res = built["books"][s["id"]]
        contract.write_bytes("/".join([out.rstrip("/"), s["id"], "positions.csv"]), positions_csv(res, book, store))
    for sid in contract.prune_removed(out, list(built["daily"]), allow_restate=allow_restate):
        print(f"{sid}: no longer published, files removed")
    contract.publish_index(out, cfg["source"], list(built["daily"]), built["summary"], updated_utc=now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), data_through=str(built["through"]))


def show(built: dict, store: prices.Prices) -> None:
    with pl.Config(tbl_rows=20, tbl_cols=14, float_precision=2, tbl_width_chars=220):
        print(built["summary"].select("strategy_id", "role", "start", "end", "buys", "closed", "wins", "pnl", "return_pct", "sharpe", "sharpe_vs_bills", "max_dd_pct", "beta_spy", "alpha_pct_yr", "alpha_t"))
    for sid, table in built["daily"].items():
        paper = table.filter(pl.col("mode") == "paper")
        book, res = built["books"][sid]
        print(f"{sid}: {table.height} days published through {table['date'].max()}, {paper.height} of them paper"
              + (f" (paper PnL {paper['pnl'].sum():+,.2f} USD)" if paper.height else "") + f"; {book} book holds {len(res.holdings)} calls worth {sum(res.holdings.values()):,.2f} USD")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="allin")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, text in (("run", "recompute and publish"), ("status", "print the book without writing anything"),
                       ("stamp", "give new calls the time they reached the ledger"), ("episodes", "append newly published episodes to the catalogue")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--data", default=str(ROOT / "data"))
        p.add_argument("--calls", default=str(ROOT / "calls.csv"))
        p.add_argument("--config", default=str(ROOT / "strategies.toml"))
        if name == "run":
            p.add_argument("--fetch", action="store_true", help="append the latest final daily prices first")
            p.add_argument("--out", default=str(ROOT / "results"), help="results root: a folder or any fsspec URI")
            p.add_argument("--allow-restate", action="store_true", help="permit changes to already published days")
        if name == "episodes":
            p.add_argument("--feed", default=episodes.FEED, help="feed address, or a saved copy of the feed")
    a = ap.parse_args(argv)

    now = dt.datetime.now(dt.timezone.utc)
    cfg = load_config(pathlib.Path(a.config))
    if a.cmd == "episodes":
        xml = pathlib.Path(a.feed).read_bytes() if pathlib.Path(a.feed).exists() else episodes.fetch_feed(a.feed)
        added = episodes.update_catalogue(pathlib.Path(a.data) / "episodes.csv", episodes.parse_feed(xml))
        for row in added:
            print(f"new episode: {row[0]} {row[2]}")
        print(f"catalogue: {len(added)} episodes added")
        return 0
    if a.cmd == "stamp":
        done = stamp_calls(a.calls, cfg, now)
        check_ledger(load_calls(a.calls), cfg, now)
        print(f"stamped calls {done} at {now:%Y-%m-%dT%H:%M:%SZ}" if done else "no call is waiting for a stamp")
        return 0
    ledger = load_calls(a.calls)
    check_ledger(ledger, cfg, now)
    waiting = [c for c in ledger if is_pending(c, cfg)]
    calls = [c for c in ledger if not is_pending(c, cfg)]
    store = prices.load_prices(a.data)
    if a.cmd == "run" and a.fetch:
        added = prices.update_live(a.data, tickers_to_update(engine.plan(calls, store.sessions()), store, cfg.get("benchmark", "SPY")), now)
        print(f"prices: {len(added)} rows added" + (f", through {max(d for d, _, _ in added)}" if added else ""))
        store = prices.load_prices(a.data)
        behind = [d for d in store.sessions() if d > publishable_through(engine.plan(calls, store.sessions()), store, now)]
        if len(behind) > 3:
            raise RuntimeError(f"{len(behind)} sessions cannot be published because a held ticker has no price: first {behind[0]}. "
                               "Fix the price source, or close the call with forced_exit if the ticker stopped trading.")
    built = build(calls, store, cfg, now, load_bills(a.data))
    if a.cmd == "run":
        publish(a.out, cfg, built, store, len(ledger), allow_restate=a.allow_restate, now_utc=now, calls_path=a.calls, config_path=a.config)
    show(built, store)
    for c in waiting:
        print(f"call {c.call_id} ({c.speaker}: {c.call}) is on file and not traded yet: it has no stamp")
    if a.cmd == "status":
        for sid, (book, res) in built["books"].items():
            print(f"\n{sid}, {book} book at the close of {res.dates[-1]}:")
            print(positions_csv(res, book, store).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
