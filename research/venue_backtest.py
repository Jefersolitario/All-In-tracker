"""Research only: enter each call on a 24/7 venue shortly after its episode instead of at the rule's close.
argv[1] = tracker repository (trusted code), argv[2] = folder with candles.csv from the exchanges (data only)."""
import sys, csv, datetime as dt, statistics, math, collections, json
from zoneinfo import ZoneInfo
root, data = sys.argv[1], sys.argv[2]
sys.path.insert(0, root + "/src")
from allin import ledger, prices, engine
NY = ZoneInfo("America/New_York"); UTC = dt.timezone.utc
calls = {c.call_id: c for c in ledger.load_calls(root + "/calls.csv")}
store = prices.load_prices(root + "/data"); sessions = store.sessions()
base = {p.call.call_id: p for p in engine.plan(list(calls.values()), sessions)}
res = engine.simulate([p for p in base.values() if p.entry is not None], store, 10_000.0, end=dt.date(2026, 11, 17))
n_active = dict(zip(res.dates, res.n_active))

cand = collections.defaultdict(list)   # (call, venue, symbol) -> [(open_time, open, close, interval)]
for r in csv.DictReader(open(data + "/candles.csv", encoding="utf-8")):
    t = dt.datetime.strptime(r["open_time_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    cand[(int(r["call_id"]), r["venue"], r["symbol"], r["ledger_ticker"])].append((t, float(r["open"]), float(r["close"]), r["interval"]))
for v in cand.values():
    v.sort()
STEP = {"1h": dt.timedelta(hours=1), "4h": dt.timedelta(hours=4), "1d": dt.timedelta(days=1)}
SKIP = {"DASHUSDT", "DASHUSDC"}        # the Dash coin, not DoorDash

def price_at(series, when):
    """Price at `when`: the close of the last candle that ends at or before it."""
    best = None
    for t, o, c, iv in series:
        if t + STEP[iv] <= when:
            best = (t + STEP[iv], c, iv)
        else:
            break
    return best

def first_open(series, when):
    for t, o, c, iv in series:
        if t >= when:
            return (t, o, iv)
    return None

def close_time(ticker, day):
    if ticker.endswith("-USD") and ticker in ("BTC-USD", "SOL-USD", "TAO-USD"):
        return dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0), UTC)      # the store uses the UTC day's close for crypto
    return dt.datetime.combine(day, dt.time(16), NY).astimezone(UTC)

def pre_close(pub):
    ny = pub.astimezone(NY); best = None
    for s in sessions:
        if dt.datetime.combine(s, dt.time(16), NY) <= ny: best = s
        else: break
    return best

rows = []
for cid, p in sorted(base.items()):
    c = p.call
    if p.entry is None and cid != 50:
        continue
    pub = c.published_utc; E = p.entry
    legs = {}
    for (k, venue, sym, tick), series in cand.items():
        if k != cid or sym in SKIP:
            continue
        rank = (0 if venue == "hyperliquid" and sym.startswith("xyz:") else 1 if venue == "binance" and sym.endswith("USDT") else 2 if venue == "hyperliquid" else 3)
        if tick.endswith("-USD") and tick in ("BTC-USD", "SOL-USD", "TAO-USD"):
            rank = 0 if (venue == "binance" and sym.endswith("USDT")) else 5
        legs.setdefault(tick, []).append((rank, venue, sym, series))
    if not legs:
        continue
    out = {"id": cid, "who": c.speaker, "pub_ny": pub.astimezone(NY).strftime("%a %d %b %Y %H:%M"), "entry": str(E), "legs": [], "weight_pct": round(100 / n_active[E], 1) if E in n_active else None}
    tot_w = 0.0; g1 = g12 = 0.0; ok12 = True
    for tick, w in c.legs:
        if tick not in legs:
            continue
        for rank, venue, sym, series in sorted(legs[tick], key=lambda x: x[0]):
            early = first_open(series, pub + dt.timedelta(hours=1)); late = first_open(series, pub + dt.timedelta(hours=12))
            at = price_at(series, close_time(tick, E)) if E else None
            leg = {"t": tick, "venue": venue, "sym": sym, "iv": series[0][3], "early_at": early and early[0].strftime("%m-%d %H:%M"), "early_px": early and early[1]}
            if E and early and at and at[0] > early[0]:
                leg["rule_at"] = at[0].strftime("%m-%d %H:%M"); leg["rule_px"] = at[1]; leg["gain1h"] = 100 * (at[1] / early[1] - 1)
                leg["gain12h"] = 100 * (at[1] / late[1] - 1) if late and at[0] > late[0] else None
                pc = pre_close(pub); ref = price_at(series, close_time(tick, pc)) if pc else None
                leg["venue_pre_to_rule"] = 100 * (at[1] / ref[1] - 1) if ref else None
            out["legs"].append(leg)
        best = next((l for l in out["legs"] if l["t"] == tick and "gain1h" in l), None)
        if best:
            tot_w += abs(w); g1 += abs(w) * best["gain1h"]
            if best["gain12h"] is None: ok12 = False
            else: g12 += abs(w) * best["gain12h"]
    if tot_w:
        out["gain1h"] = g1 / tot_w; out["gain12h"] = g12 / tot_w if ok12 else None; out["covered"] = round(tot_w / sum(abs(w) for _, w in c.legs), 2)
        pc = pre_close(pub); v = engine.call_value(c, pc, E, store) if pc and pc < E else None
        out["cash_gap"] = 100 * (v - 1) if v else None
        a, b = store.on("SPY", pc), store.on("SPY", E)
        out["spy_gap"] = 100 * (b / a - 1) if a and b and pc < E else None
        out["after_close"] = pub.astimezone(NY).time() >= dt.time(16) or pub.astimezone(NY).weekday() >= 5 or pc == pre_close(pub - dt.timedelta(hours=7))
    rows.append(out)

def f(x, n=2): return "   n/a" if x is None else f"{x:+6.{n}f}"
print(f"{'id':>3} {'published (NY)':22} {'entry':10} {'legs on venue':34} {'cov':>4} {'+1h':>6} {'+12h':>6} {'cash':>6} {'venue':>6} {'SPY':>6} {'w%':>4}")
for r in rows:
    if "gain1h" not in r:
        print(f"{r['id']:>3} {r['pub_ny']:22} {r['entry']:10} no usable window:", [(l['sym'], l['iv'], l['early_at']) for l in r["legs"]][:3]); continue
    used = {}
    for l in r["legs"]:
        if "gain1h" in l and l["t"] not in used: used[l["t"]] = l
    vp = [l.get("venue_pre_to_rule") for l in used.values()]
    vmean = None if any(x is None for x in vp) else sum(vp) / len(vp)
    print(f"{r['id']:>3} {r['pub_ny']:22} {r['entry']:10} {','.join(l['sym'].replace('xyz:','') + '/' + l['iv'] for l in used.values())[:34]:34} {r['covered']:>4} {f(r['gain1h'])} {f(r['gain12h'])} {f(r['cash_gap'])} {f(vmean)} {f(r['spy_gap'])} {r['weight_pct'] or 0:>4}")

def summ(name, rs, key="gain1h"):
    v = [r[key] for r in rs if r.get(key) is not None]
    if len(v) < 3: print(name, "n =", len(v), [round(x, 2) for x in v]); return
    t = statistics.mean(v) / statistics.stdev(v) * math.sqrt(len(v))
    print(f"{name}: n={len(v)} mean {statistics.mean(v):+.2f}% median {statistics.median(v):+.2f}% up {sum(1 for x in v if x > 0)}/{len(v)} t={t:.2f} min {min(v):+.1f} max {max(v):+.1f}")
ok = [r for r in rows if "gain1h" in r]
crypto = [r for r in ok if all(t.endswith("-USD") and t[:3] in ("BTC", "SOL", "TAO") for t, _ in calls[r["id"]].legs)]
stocks = [r for r in ok if r not in crypto]
print(); summ("all venue-priced calls, enter 1h after the episode", ok); summ("  same, 12h after", ok, "gain12h")
summ("stocks and commodities", stocks); summ("  same, 12h after", stocks, "gain12h"); summ("crypto", crypto)
summ("stocks published after the close", [r for r in stocks if r["after_close"]])
for r in ok: r["x"] = r["gain1h"] - r["spy_gap"] if r.get("spy_gap") is not None else None
summ("stocks after the close, minus SPY's move over the same gap", [r for r in stocks if r["after_close"]], "x")
summ("cash gap of the same stock calls (close before the episode to the rule's close)", stocks, "cash_gap")
book = sum(r["gain1h"] * r["weight_pct"] / 100 * r["covered"] for r in ok if r["weight_pct"])
print(f"\nadded to the book, all venue-priced calls (weight at entry x gain): {book:+.2f}% of the book in total")
print(f"  stocks and commodities only: {sum(r['gain1h'] * r['weight_pct'] / 100 * r['covered'] for r in stocks if r['weight_pct']):+.2f}%   crypto only: {sum(r['gain1h'] * r['weight_pct'] / 100 * r['covered'] for r in crypto if r['weight_pct']):+.2f}%")
# cross-check Hyperliquid against Binance where both priced the same leg
print("\ncross-check, same leg on both venues (gain 1h after):")
for r in rows:
    by = collections.defaultdict(dict)
    for l in r["legs"]:
        if "gain1h" in l: by[l["t"]][l["venue"] + ":" + l["sym"]] = round(l["gain1h"], 2)
    for t, d in by.items():
        if len({k.split(":")[0] for k in d}) > 1: print("  ", r["id"], t, d)
json.dump(rows, open(data + "/result.json", "w"), default=str)
