# All-In tracker

Copy-trades the investment calls made on the All-In podcast under one mechanical rule. The repository holds the ledger of calls, the backtest, a paper-trading book that advances every day, and the published results in the format the [portfolio dashboard](https://github.com/Jefersolitario/portfolio-trading-strategies) reads.

## The rule

- **Entry:** the close of the first US session after the episode is published (New York date). A call written into the ledger later than that enters at the close of the first session after it was recorded, so nothing is bought at a price that was already known.
- **Exit:** the close one calendar year after the entry, rolled forward to a session.
- **Sizing:** one 10,000 USD portfolio shared equally by every call still inside its 12 months. Weights are reset to equal only when a call starts or ends.
- **Instruments:** the asset the speaker names. A sector or theme with no ticker gets the most liquid US-listed fund for it (`XLE`, `XLU`, `SMH`, `GLD`, `CPER`, `URA`, `SPY`). A basket is bought once in equal parts and held. Alphabet is `GOOG`.
- **Returns:** total return (dividends reinvested), before costs.

The entry and exit conventions were recovered from an earlier backtest by matching its 45 printed call returns: 40 match to the decimal, 4 are within 0.35 points, and one (GEODNET) has no price history and is left out.

## What the evidence says (19 Apr 2021 to 18 Nov 2026)

| | All-In, 12-month hold | SPY, dividends reinvested |
|---|---|---|
| Total return | +121.9% | +89.2% |
| Sharpe, returns over 3-month T-bills | 0.53 | 0.56 |
| Sharpe as the dashboard computes it (daily USD PnL) | 0.57 | 0.75 |
| Worst drawdown | -54.1% | -24.5% |
| Closed calls that made money | 19 of 29 | |

- **It is market exposure, not selection skill.** Beta to SPY is 1.17. What is left after that is +3.2% a year with a t statistic of 0.38.
- **Out of sample it lost twice what the market did.** The earlier backtest stopped on 25 Sep 2026. Over the 38 sessions to 18 Nov the recovered ledger lost 10.1% against 5.3% for SPY. With the four calls added on 19 Nov it lost 9.9%.
- **The ledger was built after the fact.** All 49 calls were picked out of transcripts by screening, the last four on 19 Nov 2026. Only the paper book, which starts on 19 Nov 2026, is free of that.

For these reasons the strategy is published with `role = "monitor"`: the dashboard shows it and keeps it out of the portfolio figures.

## Files

| Path | What it is |
|---|---|
| `calls.csv` | The ledger: one line per call. This is the only file you edit to trade a new call. |
| `strategies.toml` | Start dates, starting cash and the published variants. |
| `data/prices_snapshot.csv.gz` | Frozen total-return price levels to 18 Nov 2026 (`data/SOURCES.md` says where each came from). |
| `data/prices_live.csv` | Daily levels appended by the scheduled job. Rows are never rewritten. |
| `data/episodes.csv` | Catalogue of 446 episodes with publication times. |
| `data/tbill_3m.csv` | Three-month Treasury bill yield (FRED series DTB3), for the Sharpe over bills. |
| `src/allin/` | `ledger` (calendar rule), `prices` (store and live update), `engine` (portfolio), `cli` (publisher), `contract` (results format). |
| `results/` | Published output: `index.json`, `backtest_summary.parquet`, `allin_12m/daily.parquet`, `manifest.json`, `positions.csv`. |

## Adding a call

Append one line to `calls.csv`:

```
call_id,episode_guid,published_utc,speaker,call,legs,status,recorded_utc,recovered_return_pct,forced_exit,note
50,<guid from data/episodes.csv>,2026-11-21T02:10:00Z,Friedberg,Uranium,URA:1,active,2026-11-23T08:00:00Z,,,Says he is buying uranium miners; URA is the proxy.
```

- `legs` are `TICKER:weight` pairs separated by `;`. Absolute weights sum to 1; a negative weight is a short.
- `recorded_utc` is the time you add the line. It is required for every episode published on or after the paper start.
- `forced_exit` (a date) closes a call early when its ticker stops trading. Leave it blank otherwise.
- `status = excluded` keeps a call on record without trading it.
- Never edit the legs or dates of a call that has already entered. Published days cannot change, and the run fails if they would.

A disclosed sale on air does not close a call: the rule is 12 months. Record it in `note`.

## How it runs

`.github/workflows/paper.yml` runs at 01:07 UTC from Tuesday to Saturday, with a second slot at 07:07, and whenever `calls.csv` or `strategies.toml` changes on `main`. It downloads daily bars from Yahoo Finance for every ticker still held, appends only bars that are final, recomputes everything from the ledger and the stored prices, and commits `data/prices_live.csv` and `results/`.

- A day is published only when every leg held that day has its final price for it.
- A stored price is never replaced: new levels are chained from the last stored one with ratios of adjusted closes, so a restatement by the data vendor cannot move a published day.
- Samsung and SK Hynix are held as dollar series built from the Seoul listings and the won-dollar rate.
- The paper book starts in cash on `paper_start`, buys the calls that are open at that close in equal parts, and sells each on its original exit day.

```
pip install -e ".[dev]"
pytest -q                         # 25 tests
python -m allin.cli status        # the book as it stands: what is held, since when, until when
python -m allin.cli run           # recompute and write ./results from stored prices
python -m allin.cli run --fetch   # the same after appending the latest final prices
```

## What has and has not been checked

- The portfolio engine is tested against a second, independent implementation and against the recovered figures (24,634 USD on 25 Sep 2026 without GEODNET; the original reported 25,008 with it).
- The live price download has been tested with a stand-in for the data vendor, not against Yahoo itself: the build environment could not reach it. The first scheduled run is the real test, and it fails loudly rather than publish a partial day.
- The frozen price snapshot was assembled from public GitHub copies of Yahoo data. Every US-listed ticker held since 25 Sep 2026 was cross-checked against a second copy.
- The calls dated after 30 Sep 2026 were read from machine-made transcripts by a language model. Three episodes in that stretch (28 Sep, 30 Sep, 2 Oct) could only be read as third-party summaries.
- `reproduce_strategy.MD` is the earlier handoff note kept for the record; it holds the episode catalogue and no strategy results.
