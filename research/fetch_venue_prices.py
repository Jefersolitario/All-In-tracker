"""Research only: public candles from Hyperliquid and Binance around each call in calls.csv.

Runs in the venue-prices workflow on its own branch. Uses only public market-data endpoints: no key, no account.
Writes research/venues/candles.csv and research/venues/status.json. Nothing here is read by the paper job.
"""
import csv, datetime as dt, json, pathlib, sys, time, urllib.error, urllib.parse, urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "research" / "venues"
UA = {"User-Agent": "Mozilla/5.0 (research; all-in-tracker)", "Accept": "application/json"}
HL = "https://api.hyperliquid.xyz/info"
BINANCE_SPOT = ["https://data-api.binance.vision", "https://api.binance.com", "https://api1.binance.com", "https://api-gcp.binance.com"]
BINANCE_FUT = ["https://fapi.binance.com", "https://fapi1.binance.com"]
ALIASES = {  # ledger ticker -> names the underlying may go by on a venue
    "GOOG": ["GOOG", "GOOGL"], "CPER": ["COPPER", "HG", "XCU", "CPER"], "SPY": ["SP500", "SPX", "US500", "SPY", "S&P500"],
    "QQQ": ["XYZ100", "NDX", "NAS100", "US100", "QQQ"], "GLD": ["GOLD", "XAU", "GLD", "PAXG"], "BTC-USD": ["BTC"], "SOL-USD": ["SOL"],
    "TAO-USD": ["TAO"], "SMH": ["SMH", "SEMI", "SOX", "SEMIS"], "SPCX": ["SPCX", "SPACEX"], "SKH-USD": ["SKHX", "SKHYNIX", "HYNIX", "SKH"],
    "SAMSUNG-USD": ["SAMSUNG", "SMSN", "SSNLF"], "URA": ["URANIUM", "URA"], "XLE": ["XLE", "OIL", "WTI", "CL"], "XLU": ["XLU"],
}
status = {"started_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "requests": [], "errors": []}


def http(url, body=None, note=""):
    data = json.dumps(body).encode() if body is not None else None
    headers = dict(UA, **({"Content-Type": "application/json"} if body is not None else {}))
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            msg = f"{note or url}: HTTP {e.code} {e.read()[:200]!r}"
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(3 * (attempt + 1)); continue
            status["errors"].append(msg); return None
        except Exception as e:  # noqa: BLE001
            if attempt < 2:
                time.sleep(3 * (attempt + 1)); continue
            status["errors"].append(f"{note or url}: {type(e).__name__}: {e}"); return None


def ms(t):
    return int(t.timestamp() * 1000)


def iso(m):
    return dt.datetime.fromtimestamp(m / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_calls():
    out = []
    for r in csv.DictReader(open(ROOT / "calls.csv", encoding="utf-8")):
        if r["status"] != "active" or not r["published_utc"]:
            continue
        pub = dt.datetime.strptime(r["published_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
        legs = [x.split(":")[0] for x in r["legs"].split(";") if x]
        out.append({"id": int(r["call_id"]), "pub": pub, "legs": legs})
    return out


def names_for(ticker):
    return [n.upper() for n in ALIASES.get(ticker, [ticker])]


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    calls = load_calls(); now = dt.datetime.now(dt.timezone.utc); rows = []
    tickers = sorted({t for c in calls for t in c["legs"]})

    # ---- Hyperliquid: every perp dex and its universe
    dexs = http(HL, {"type": "perpDexs"}, "hl perpDexs")
    dex_names = [""] + [d["name"] for d in (dexs or []) if isinstance(d, dict) and d.get("name")]
    status["hl_dexs"] = dex_names
    universe = {}
    for dex in dex_names:
        meta = http(HL, {"type": "meta", "dex": dex}, f"hl meta {dex or 'main'}")
        names = [u.get("name") for u in (meta or {}).get("universe", []) if isinstance(u, dict) and not u.get("isDelisted")]
        universe[dex or "main"] = names
        time.sleep(0.2)
    status["hl_universe"] = universe
    hl_map = {}
    for t in tickers:
        want = set(names_for(t))
        hits = [n for dex, names in universe.items() for n in names if n and n.split(":")[-1].upper() in want and (dex != "main" or t.endswith("-USD"))]
        if hits:
            hl_map[t] = hits
    status["hl_map"] = hl_map

    def hl_candles(coin, start, end):
        for iv in ("1h", "4h", "1d"):
            got = http(HL, {"type": "candleSnapshot", "req": {"coin": coin, "interval": iv, "startTime": ms(start), "endTime": ms(end)}}, f"hl candles {coin} {iv} {start:%Y-%m-%d}")
            time.sleep(0.25)
            if got:
                return iv, [(c["t"], c["o"], c["h"], c["l"], c["c"], c.get("v", "")) for c in got if isinstance(c, dict) and "t" in c]
        return None, []

    # ---- Binance: which hosts answer, and which symbols exist
    spot_base, spot_syms = None, {}
    for base in BINANCE_SPOT:
        info = http(base + "/api/v3/exchangeInfo", note=f"binance spot exchangeInfo {base}")
        if info and info.get("symbols"):
            spot_base = base
            spot_syms = {s["symbol"]: (s["baseAsset"], s["quoteAsset"], s["status"]) for s in info["symbols"]}
            break
    fut_base, fut_syms = None, {}
    for base in BINANCE_FUT:
        info = http(base + "/fapi/v1/exchangeInfo", note=f"binance futures exchangeInfo {base}")
        if info and info.get("symbols"):
            fut_base = base
            fut_syms = {s["symbol"]: (s.get("baseAsset", ""), s.get("quoteAsset", ""), s.get("status", ""), s.get("underlyingType", ""), s.get("contractType", "")) for s in info["symbols"]}
            break
    status["binance_spot_base"] = spot_base; status["binance_spot_symbols"] = len(spot_syms)
    status["binance_fut_base"] = fut_base; status["binance_fut_symbols"] = len(fut_syms)
    stock_like = sorted({t for t in tickers if not t.endswith("-USD")} | {"GOOGL", "NVDA", "TSLA", "AAPL", "MSFT", "AMZN", "META"})

    def variants(n):
        return {n, n + "B", "B" + n, n + "X", n + "ON", n + "ST", "T" + n, n + "STOCK"}
    bn_map = {}
    for t in tickers:
        hits = []
        for n in names_for(t):
            for sym, (b, q, st) in spot_syms.items():
                if q in ("USDT", "USDC", "FDUSD", "USD1", "USD") and (b.upper() in variants(n) if not t.endswith("-USD") else b.upper() == n):
                    hits.append(["spot", sym, st])
            for sym, v in fut_syms.items():
                if v[1] in ("USDT", "USDC") and v[0].upper() in ({n} if t.endswith("-USD") else variants(n)) and v[4] in ("PERPETUAL", "TRADIFI_PERPETUAL", ""):
                    hits.append(["perp", sym, v[2]])
        if hits:
            bn_map[t] = hits
    status["binance_map"] = bn_map
    # what stock-looking base assets exist at all, to learn the naming
    status["binance_spot_stocklike"] = sorted({b for b, q, st in spot_syms.values() if any(s in b.upper() for s in stock_like if len(s) >= 3) and len(b) <= 9})[:200]
    status["binance_fut_stocklike"] = sorted({s for s, v in fut_syms.items() if any(k in v[0].upper() for k in stock_like if len(k) >= 3) or "TRADIFI" in str(v[3]).upper() or "TRADIFI" in str(v[4]).upper()})[:200]
    status["binance_fut_types"] = sorted({f"{v[3]}|{v[4]}" for v in fut_syms.values()})

    def bn_klines(market, sym, start, end):
        base, path = (spot_base, "/api/v3/klines") if market == "spot" else (fut_base, "/fapi/v1/klines")
        if not base:
            return None, []
        out, cur = [], ms(start)
        while cur < ms(end):
            q = urllib.parse.urlencode({"symbol": sym, "interval": "1h", "startTime": cur, "endTime": ms(end), "limit": 1000})
            got = http(f"{base}{path}?{q}", note=f"binance {market} klines {sym} {start:%Y-%m-%d}")
            time.sleep(0.15)
            if not got:
                break
            out += [(k[0], k[1], k[2], k[3], k[4], k[5]) for k in got]
            if len(got) < 1000:
                break
            cur = got[-1][0] + 1
        return "1h", out

    # ---- candles around every call
    coverage = {}
    for c in calls:
        start = c["pub"] - dt.timedelta(days=2); end = min(c["pub"] + dt.timedelta(days=8), now)
        cov = []
        for t in c["legs"]:
            for coin in hl_map.get(t, []):
                iv, got = hl_candles(coin, start, end)
                rows += [("hyperliquid", "perp", coin, iv, iso(k[0]), *k[1:], c["id"], t) for k in got]
                cov.append(["hyperliquid", coin, iv, len(got)])
            for market, sym, st in bn_map.get(t, []):
                iv, got = bn_klines(market, sym, start, end)
                rows += [("binance", market, sym, iv, iso(k[0]), *k[1:], c["id"], t) for k in got]
                cov.append(["binance", market + ":" + sym, iv, len(got)])
        coverage[c["id"]] = {"published_utc": c["pub"].strftime("%Y-%m-%dT%H:%M:%SZ"), "legs": c["legs"], "got": cov}
    status["coverage"] = coverage
    status["rows"] = len(rows)
    with open(OUT / "candles.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(("venue", "market", "symbol", "interval", "open_time_utc", "open", "high", "low", "close", "volume", "call_id", "ledger_ticker"))
        w.writerows(rows)
    status["errors"] = status["errors"][:300]
    (OUT / "status.json").write_text(json.dumps(status, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({k: status[k] for k in ("hl_dexs", "hl_map", "binance_spot_base", "binance_fut_base", "binance_map", "rows")}, indent=1)[:6000])
    print("errors:", len(status["errors"])); [print("  ", e[:240]) for e in status["errors"][:25]]
    return 0


if __name__ == "__main__":
    sys.exit(main())
