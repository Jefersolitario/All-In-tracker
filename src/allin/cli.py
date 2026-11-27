"""Command line: build the backtest, advance the paper book and publish the results contract.

    python -m allin.cli run                 recompute from stored prices, write ./results
    python -m allin.cli run --fetch         first append the latest final daily prices
    python -m allin.cli run --out gs://bucket/all-in-tracker      publish somewhere else (needs gcsfs)
    python -m allin.cli status              print the book as it stands: what is held, since when, until when
    python -m allin.cli stamp               give new calls the time they reached the ledger (only the scheduled job runs this)
    python -m allin.cli episodes            append newly published episodes to data/episodes.csv from the publisher's feed
"""
from __future__ import annotations

import argparse
import bisect
import csv
import datetime as dt
import hashlib
import io
import json
import math
import os
import pathlib
import re
import tomllib

import polars as pl

from . import contract, engine, episodes, prices, venue
from .ledger import COLUMNS, Call, field_spans, load_calls, plus_year

ROOT = pathlib.Path(__file__).resolve().parents[2]
DAYS_PER_YEAR = 252
STAMPS = "stamps.csv"                      # in the data folder: which call the scheduled job stamped, and when
STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
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
        s["entry"] = s.get("entry", "close")
        if s["entry"] not in ("close", "venue"):
            raise ValueError(f"{s['id']}: entry must be 'close' or 'venue'")
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


def load_stamps(path: str | pathlib.Path) -> dict[int, str]:
    """The stamps the scheduled job has written, by call id. A call stamped twice keeps the later line."""
    path = pathlib.Path(path)
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as f:
        return {int(r["call_id"]): r["recorded_utc"] for r in csv.DictReader(f)}


def check_stamps(calls: list[Call], cfg: dict, stamps: dict[int, str]) -> None:
    """Every stamp on a new call must be the one the scheduled job wrote.

    A call is bought at the close of the first session after its stamp. The job writes the stamp when the line reaches
    `main`, so that close is always still to come. A stamp typed by hand, or changed afterwards, can point at a close
    that is already known, and nothing else in the pipeline could tell the difference.
    """
    for c in calls:
        if c.status == "active" and c.call_id > cfg["recovered_calls_through"] and c.recorded_utc is not None:
            if stamps.get(c.call_id) != c.recorded_utc.strftime(STAMP_FORMAT):
                raise ValueError(f"call {c.call_id}: its recorded_utc was not written by the scheduled job ({STAMPS} has {stamps.get(c.call_id) or 'no line for it'}). "
                                 "Empty the field and the job stamps the call at its next run.")


def stamp_calls(path: str | pathlib.Path, cfg: dict, now_utc: dt.datetime, log_path: str | pathlib.Path) -> list[int]:
    """Write the current time into `recorded_utc` of every new call that has none, and note it in the stamp log.
    Returns the ids stamped.

    A call is bought at the close of the first session after its stamp, so the stamp has to be the moment the line
    became part of the live ledger. The scheduled job runs this on `main`; nothing else should fill the field in.
    Only the empty field is filled: every other byte of the file, line endings and quoting included, stays as it was.
    """
    path = pathlib.Path(path)
    lines = re.findall(r"[^\n]*\n|[^\n]+$", path.read_bytes().decode("utf-8"))
    if not lines or tuple(next(csv.reader([lines[0].rstrip("\r\n")]), ())) != COLUMNS:
        raise ValueError(f"{path.name} does not start with the expected header line")
    i_id, i_status, i_rec = COLUMNS.index("call_id"), COLUMNS.index("status"), COLUMNS.index("recorded_utc")
    stamp, done = now_utc.strftime(STAMP_FORMAT), []
    for n in range(1, len(lines)):
        body = lines[n].rstrip("\r\n"); ending = lines[n][len(body):]
        if not body.strip():
            continue                                                  # a blank line
        try:
            spans = field_spans(body)
        except ValueError as e:
            raise ValueError(f"{path.name} line {n + 1}: {e}") from None
        if len(spans) != len(COLUMNS):
            raise ValueError(f"{path.name} line {n + 1}: expected {len(COLUMNS)} fields, found {len(spans)} (a comma inside a field needs double quotes)")
        value = lambda i: next(csv.reader([body[spans[i][0]:spans[i][1]]]), [""])[0].strip() if body[spans[i][0]:spans[i][1]] else ""    # noqa: E731
        try:
            call_id = int(value(i_id))
        except ValueError:
            raise ValueError(f"{path.name} line {n + 1}: call_id is not a whole number") from None
        if call_id > cfg["recovered_calls_through"] and value(i_status) == "active" and not value(i_rec):
            lines[n] = body[:spans[i_rec][0]] + stamp + body[spans[i_rec][1]:] + ending
            done.append(call_id)
    if done:
        log = pathlib.Path(log_path)
        text = log.read_text(encoding="utf-8") if log.exists() else ""
        with log.open("a", encoding="utf-8", newline="") as f:       # the log first: a line here without a stamp in the ledger is harmless
            f.write("call_id,recorded_utc\n" if not text else "" if text.endswith("\n") else "\n")
            f.writelines(f"{k},{stamp}\n" for k in done)
        path.write_bytes("".join(lines).encode("utf-8"))
    return done


def tickers_to_update(positions: list[engine.Position], store: prices.Prices, benchmark: str) -> tuple[list[str], set[str]]:
    """What the daily job downloads: the benchmark, and the legs of every call that is still open or has not entered yet.
    Also returns the tickers needed only by calls whose forced exit is at hand: such a ticker may have stopped trading,
    so failing to download it is not an error."""
    last = store.sessions()[-1]
    always, normal, forced = {benchmark, prices.CALENDAR}, set(), set()
    for p in positions:
        legs = [t for t, _ in p.call.legs]
        if p.exit is None or p.exit > last or (p.exit == last and any(store.on(t, last) is None for t in legs)):
            at_hand = p.call.forced_exit is not None and (p.call.forced_exit - last).days <= engine.MAX_STALE_DAYS
            (forced if at_hand else normal).update(legs)
    return sorted(always | normal | forced), forced - normal - always


def check_not_stalled(store: prices.Prices, through: dt.date, positions: list[engine.Position] = (), limit: int = 3) -> None:
    """Fail when sessions pile up that cannot be published, which means a held ticker has stopped getting prices."""
    behind = [d for d in store.sessions() if d > through]
    if len(behind) > limit:
        who = ", ".join(engine.incomplete_legs(list(positions), store, behind[0])) or "a held ticker"
        raise RuntimeError(f"{len(behind)} sessions cannot be published: since {behind[0]} there is no price for {who}. "
                           "If the vendor is late, the next run catches up. If the ticker stopped trading, give its call a forced_exit on that day. "
                           "If it still trades and the vendor's history has a gap (seen after a stock split), add the missing closes by hand: "
                           "see 'A ticker that still trades but gets no bars' in the README.")


def missing_bars(positions: list[engine.Position], store: prices.Prices, now_utc: dt.datetime) -> list[str]:
    """Held tickers that have no bar for the latest stored session although that bar is final by now.

    The job stops only when more than three sessions are stuck, which is close to a week after the first missing bar.
    This says so from the first run: one late bar is ordinary, the same ticker missing again the next day is not."""
    sessions = store.sessions(); last = sessions[-1]; out = []
    for t in engine.incomplete_legs(list(positions), store, last):
        seen = store.last(t)
        if prices.final_through(t, now_utc) < last or (seen is not None and seen >= last):
            continue                                                  # its day is not over yet, or it already has a later bar
        n = len([d for d in sessions if seen is not None and d > seen])
        where = f"the last one stored is {seen}, {n} session{'s' if n != 1 else ''} back" if seen is not None else "none is stored at all"
        out.append(f"{t} has no bar for {last}: {where}. A late bar arrives with the next run; "
                   "a ticker that still trades and stays without bars needs its closes added by hand (see the README).")
    return out


def stale_legs(res: engine.Result, store: prices.Prices, days: int = 4) -> list[str]:
    """Carried prices worth a look: a held leg whose latest price is more than `days` old at the last close."""
    end = res.dates[-1]; out = []
    for k in sorted(res.holdings):
        for ticker, _ in res.positions[k].call.legs:
            seen = [d for d in store._dates.get(ticker, []) if d <= end]
            if seen and (end - seen[-1]).days > days:
                out.append(f"call {k}: {ticker} has no price since {seen[-1]}; it is carried at that close")
    return out


def publishable_through(positions: list[engine.Position], store: prices.Prices, now_utc: dt.datetime) -> dt.date:
    """Last session that can be published for good: every leg held that day has its final price for that day."""
    done = engine.complete_through(positions, store)
    last = store.sessions()[-1]
    live = {t for p in positions if p.entry is not None and (p.exit is None or p.exit >= last) for t, _ in p.call.legs}
    final = min([prices.final_through(t, now_utc) for t in live | {prices.CALENDAR}])
    return max(d for d in store.sessions() if d <= min(done, final))


def last_published(out: str) -> dt.date | None:
    """The last day already published under a results root, or None when nothing is there yet."""
    raw = contract.read_bytes("/".join([out.rstrip("/"), "index.json"]))
    day = json.loads(raw).get("data_through") if raw else None
    return dt.date.fromisoformat(day) if day else None


def check_book_unchanged(old: pl.DataFrame | None, new: pl.DataFrame, tol: float = 0.01) -> None:
    """A call cannot join or leave on a day that is already published. The shared contract check compares PnL only,
    and a call slipped in at a published close leaves that day's PnL untouched, so trades and capital are compared too."""
    if old is None or old.height == 0:
        return
    j = old.select("date", pl.col("n_trades").alias("n_old"), pl.col("capital").alias("cap_old")).join(new, on="date", how="inner")
    bad = j.filter((pl.col("n_trades") != pl.col("n_old")) | ((pl.col("capital") - pl.col("cap_old")).abs() > tol))
    if bad.height:
        raise RuntimeError(f"{bad.height} published days would change in trades or capital, first {bad['date'][0]}. "
                           "A call cannot enter or leave on a published day. Re-run with --allow-restate only if this is intended.")


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
    y = [equity[i] / equity[i - 1] - 1 if equity[i - 1] > 0 else 0.0 for i in range(1, len(equity))]
    x = [bench[i] / bench[i - 1] - 1 for i in range(1, len(bench))]
    n = len(y)
    mx, my = (sum(x) / n, sum(y) / n) if n else (0.0, 0.0)
    sxx = sum((a - mx) ** 2 for a in x)
    if n < 30 or sxx == 0:
        return {"beta_spy": None, "alpha_pct_yr": None, "alpha_t": None}
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
    closed = [p for p in entered if (p.exit is not None and p.exit <= end) or p.call.call_id in res.wiped]
    wins = sum(1 for p in closed if p.call.call_id not in res.wiped and engine.call_value(p.call, p.entry, p.exit, store) > 1)
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
                    p.entry, leaves, f"{usd:.2f}", f"{100 * usd / total:.2f}" if total > 0 else "0.00", f"{100 * (engine.call_value(p.call, p.entry, end, store) - 1):.2f}"))
    return buf.getvalue().encode()


def build(calls: list[Call], store: prices.Prices, cfg: dict, now_utc: dt.datetime, bills: list[tuple[dt.date, float]] | None = None,
          entries: list[venue.Entry] | None = None) -> dict:
    """Everything that gets published, computed from the ledger, the stored prices and the venue buys on file."""
    bills = bills or []; entries = entries or []
    sessions = store.sessions()
    through = publishable_through(engine.plan(calls, sessions), store, now_utc)
    bt_end = max((d for d in sessions if d < cfg["paper_start"] and d <= through), default=None)
    bought_early = venue.factors(entries, calls)
    if any(s.get("entry") == "venue" for s in cfg["strategy"]):
        late = venue.not_looked_up(entries, calls, sessions, cfg["recovered_calls_through"], through)
        if late:
            raise RuntimeError(f"calls {late} have entered and their venue prices are not in {venue.ENTRIES}. Run with --fetch: the job looks them up before it publishes the day.")
    bench_t = cfg.get("benchmark", "SPY")
    daily, rows, books = {}, [], {}
    for s in cfg["strategy"]:
        mine = [c for c in calls if FILTERS[s.get("calls", "all")](c)]
        table = pl.DataFrame(schema=contract.DAILY_SCHEMA); book = None
        early = bought_early if s.get("entry") == "venue" else None
        plan = engine.plan(mine, sessions)
        if bt_end is not None and any(p.entry is not None and p.entry <= bt_end for p in plan):
            bt = engine.simulate(plan, store, cfg["start_cash"], end=bt_end, early=early)
            if bt.dates[0] < cfg["backtest_start"]:
                raise ValueError(f"{s['id']}: a call enters before backtest_start")
            bench = [store.on(bench_t, d) for d in bt.dates]
            rows.append(summary_row(s["id"], s["name"], s["role"], bool(s.get("paper", False)), bt, bench, cfg["start_cash"], store, bills))
            table = daily_table(bt, "backtest"); book = ("backtest", bt)
        if not s.get("paper", False):
            continue
        plan = engine.plan(mine, sessions, not_before=cfg["paper_start"])
        if any(p.entry is not None and p.entry <= through for p in plan):
            paper = engine.simulate(plan, store, cfg["start_cash"], end=through, early=early)       # the paper book starts in cash on paper_start
            table = pl.concat([table, daily_table(paper, "paper")]); book = ("paper", paper)
        if book is not None:                                                            # a strategy none of whose calls has entered yet publishes nothing
            daily[s["id"]] = table; books[s["id"]] = book
    if rows:
        first = min(r["start"] for r in rows); days = [d for d in sessions if first <= d <= bt_end]
        level = [cfg["start_cash"] * store.on(bench_t, d) / store.on(bench_t, days[0]) for d in days]
        ref_stats = engine.stats(level, DAYS_PER_YEAR, bill_returns(bills, days))
        rows.append({"strategy_id": f"{bench_t.lower()}_buy_hold", "name": f"{bench_t} bought on {days[0]} and held, dividends reinvested", "paper_traded": False, "role": "tested",
                     "start": days[0], "end": bt_end, "buys": 1, "put_in": cfg["start_cash"], "pnl": ref_stats["pnl"], "return_pct": ref_stats["return_pct"],
                     "sharpe": ref_stats["sharpe"], "sortino": ref_stats["sortino"], "max_dd": ref_stats["max_dd"], "sharpe_per_dollar": ref_stats["sharpe_per_dollar"],
                     "placebo_p": None, "sharpe_vs_bills": ref_stats.get("sharpe_vs_bills"), "max_dd_pct": ref_stats["max_dd_pct"], "closed": 0, "wins": 0,
                     "beta_spy": 1.0, "alpha_pct_yr": 0.0, "alpha_t": None})
    summary = pl.DataFrame(rows, schema_overrides={"placebo_p": pl.Float64, "sharpe_vs_bills": pl.Float64, "alpha_t": pl.Float64, "beta_spy": pl.Float64,
                                                   "alpha_pct_yr": pl.Float64, "put_in": pl.Float64, "sharpe": pl.Float64, "sortino": pl.Float64,
                                                   "sharpe_per_dollar": pl.Float64})
    return {"daily": daily, "summary": summary, "books": books, "through": through}


def publish(out: str, cfg: dict, built: dict, store: prices.Prices, n_calls: int, allow_restate: bool = False, now_utc: dt.datetime | None = None,
            calls_path: str | pathlib.Path | None = None, config_path: str | pathlib.Path | None = None) -> None:
    now_utc = now_utc or dt.datetime.now(dt.timezone.utc)
    code = fingerprint([*ROOT.glob("src/allin/*.py"), pathlib.Path(config_path or ROOT / "strategies.toml")])
    ledger = fingerprint([pathlib.Path(calls_path or ROOT / "calls.csv")])
    for s in cfg["strategy"]:
        if not s.get("paper", False) or s["id"] not in built["daily"]:
            continue
        manifest = {"id": s["id"], "name": s["name"], "market": "US-listed stocks and ETFs, with some crypto and foreign shares", "currency": "USD",
                    "days_per_year": DAYS_PER_YEAR, "backtest_start": cfg["backtest_start"], "paper_start": cfg["paper_start"], "description": s.get("description", ""),
                    "start_cash_usd": cfg["start_cash"], "code_version": code, "ledger_version": ledger, "calls_in_ledger": n_calls,
                    "source_repo": cfg["source"], "role": s["role"], "entry": s.get("entry", "close")}
        if not allow_restate:
            check_book_unchanged(contract.read_parquet("/".join([out.rstrip("/"), s["id"], "daily.parquet"])), built["daily"][s["id"]])
        contract.publish_strategy(out, manifest, built["daily"][s["id"]], allow_restate=allow_restate)
        book, res = built["books"][s["id"]]
        contract.write_bytes("/".join([out.rstrip("/"), s["id"], "positions.csv"]), positions_csv(res, book, store))
    for sid in contract.prune_removed(out, list(built["daily"]), allow_restate=allow_restate):
        print(f"{sid}: no longer published, files removed")
    contract.publish_index(out, cfg["source"], list(built["daily"]), built["summary"], updated_utc=now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), data_through=str(built["through"]))


def show(built: dict, store: prices.Prices) -> None:
    with pl.Config(tbl_rows=20, tbl_cols=14, float_precision=2, tbl_width_chars=220):
        if built["summary"].height:
            print(built["summary"].select("strategy_id", "role", "start", "end", "buys", "closed", "wins", "pnl", "return_pct", "sharpe", "sharpe_vs_bills", "max_dd_pct", "beta_spy", "alpha_pct_yr", "alpha_t"))
    for sid, table in built["daily"].items():
        paper = table.filter(pl.col("mode") == "paper")
        book, res = built["books"][sid]
        print(f"{sid}: {table.height} days published through {table['date'].max()}, {paper.height} of them paper"
              + (f" (paper PnL {paper['pnl'].sum():+,.2f} USD)" if paper.height else "") + f"; {book} book holds {len(res.holdings)} calls worth {sum(res.holdings.values()):,.2f} USD")


def main(argv: list[str] | None = None, now: dt.datetime | None = None, fetch=None, venue_fetch: dict | None = None) -> int:
    """`now`, `fetch` and `venue_fetch` stand in for the clock, the data vendor and the 24/7 venues in tests."""
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
            p.add_argument("--allow-jump", default="", help="tickers, comma separated, whose vendor data is confirmed: a move beyond half or double, "
                                                            "a currency move beyond 10%%, or a history that no longer matches what is stored")
        if name == "episodes":
            p.add_argument("--feed", default=episodes.FEED, help="feed address, or a saved copy of the feed")
    a = ap.parse_args(argv)

    now = now or dt.datetime.now(dt.timezone.utc)
    cfg = load_config(pathlib.Path(a.config))
    if a.cmd == "episodes":
        xml = pathlib.Path(a.feed).read_bytes() if pathlib.Path(a.feed).exists() else episodes.fetch_feed(a.feed)
        added = episodes.update_catalogue(pathlib.Path(a.data) / "episodes.csv", episodes.parse_feed(xml))
        for row in added:
            print(f"new episode: {row[0]} {row[2]}")
        print(f"catalogue: {len(added)} episodes added")
        return 0
    stamp_log = pathlib.Path(a.data) / STAMPS
    if a.cmd == "stamp":
        if os.environ.get("GITHUB_ACTIONS") != "true":
            raise SystemExit("stamp is run by the scheduled job on main and nowhere else: a stamp written on another machine can enter a call "
                             "at a close that is already known. Leave recorded_utc empty; the job fills it in.")
        done = stamp_calls(a.calls, cfg, now, stamp_log)
        stamped = load_calls(a.calls)
        check_ledger(stamped, cfg, now)
        check_stamps(stamped, cfg, load_stamps(stamp_log))
        print(f"stamped calls {done} at {now:%Y-%m-%dT%H:%M:%SZ}" if done else "no call is waiting for a stamp")
        return 0
    ledger = load_calls(a.calls)
    check_ledger(ledger, cfg, now)
    check_stamps(ledger, cfg, load_stamps(stamp_log))
    waiting = [c for c in ledger if is_pending(c, cfg)]
    calls = [c for c in ledger if not is_pending(c, cfg)]
    store = prices.load_prices(a.data)
    if a.cmd == "run" and a.fetch:
        need, optional = tickers_to_update(engine.plan(calls, store.sessions()), store, cfg.get("benchmark", "SPY"))
        added = prices.update_live(a.data, need, now, fetch or prices.yahoo_history, published_through=last_published(a.out), optional=optional,
                                   allow_jump={t.strip() for t in a.allow_jump.split(",") if t.strip()})
        print(f"prices: {len(added)} rows added" + (f", through {max(d for d, _, _ in added)}" if added else ""))
        store = prices.load_prices(a.data)
        plan = engine.plan(calls, store.sessions())
        for line in missing_bars(plan, store, now):
            print(f"::warning::{line}")                  # before the check below, so the run that stops says it too
        check_not_stalled(store, publishable_through(plan, store, now), plan)
        if any(s.get("entry") == "venue" for s in cfg["strategy"]):
            looked_up = venue.update(a.data, calls, store.sessions(), cfg["recovered_calls_through"], now, venue_fetch)
            for e in looked_up:
                print(f"venue: call {e.call_id} {e.ticker}: " + (f"bought at {e.entry_price:g} on {e.venue} at {e.entry_utc:%Y-%m-%d %H:%M} UTC, {e.close_price:g} when the rule buys "
                                                                 f"({100 * (e.ratio - 1):+.2f}%)" if e.ratio is not None else f"waits for the close ({e.basis})"))
    entries = venue.load_entries(a.data)
    venue.check(entries, ledger, store.sessions(), cfg["recovered_calls_through"])
    built = build(calls, store, cfg, now, load_bills(a.data), entries)
    if a.cmd == "run":
        publish(a.out, cfg, built, store, len(ledger), allow_restate=a.allow_restate, now_utc=now, calls_path=a.calls, config_path=a.config)
    show(built, store)
    for _, res in built["books"].values():
        for line in stale_legs(res, store):
            print(f"::warning::{line}")                  # shows up as a warning on the run in GitHub Actions
    for c in waiting:
        print(f"call {c.call_id} ({c.speaker}: {c.call}) is on file and not traded yet: it has no stamp")
    if a.cmd == "status":
        for sid, (book, res) in built["books"].items():
            print(f"\n{sid}, {book} book at the close of {res.dates[-1]}:")
            print(positions_csv(res, book, store).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
