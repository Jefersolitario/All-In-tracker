# All-In tracker

Copy-trades the investment calls made on the All-In podcast under one mechanical rule. The repository holds the ledger of calls, the backtest, a paper-trading book that advances every day, and the published results in the format the [portfolio dashboard](https://github.com/Jefersolitario/portfolio-trading-strategies) reads.

## The rule

- **Entry:** the close of the first US session after the episode is published (New York date). A call added to the ledger from now on enters at the close of the first session after it reached `main`, so nothing is bought at a price that was already known.
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
| `data/stamps.csv` | Which call the scheduled job stamped, and when. Only the job writes it. |
| `data/price_trust.csv` | Appears the first time you confirm a ticker's data by hand: which ticker, from which day. Only the job writes it. |
| `data/episodes.csv` | Catalogue of every episode with its release time. A workflow appends new ones from the publisher's feed. |
| `data/screening_log.csv` | One line per episode read for calls since 26 Sep 2026: what it was read from and what was found. |
| `docs/screening.md` | How new episodes are screened: what counts as a call, which instrument, what gets written. |
| `data/tbill_3m.csv` | Three-month Treasury bill yield (FRED series DTB3), for the Sharpe over bills. |
| `src/allin/` | `ledger` (calendar rule), `prices` (store and live update), `engine` (portfolio), `episodes` (catalogue), `cli` (publisher), `contract` (results format). |
| `results/` | Published output: `index.json`, `backtest_summary.parquet`, `allin_12m/daily.parquet`, `manifest.json`, `positions.csv`. |

## Adding a call

Append one line to `calls.csv` and leave `recorded_utc` empty:

```
call_id,episode_guid,published_utc,speaker,call,legs,status,recorded_utc,recovered_return_pct,forced_exit,note
50,<guid from data/episodes.csv>,2026-11-21T02:10:00Z,Friedberg,Uranium,URA:1,active,,,,Says he is buying uranium miners; URA is the proxy.
```

- `legs` are `TICKER:weight` pairs separated by `;`. Absolute weights sum to 1; a negative weight is a short.
- **`recorded_utc` is a stamp, not something you type.** The daily job fills it in with the time the line reached `main`, and notes it in `data/stamps.csv`. Until then the call is on file and not traded. It is bought at the close of the first US session after the stamp's New York date, so a line merged before midnight New York time trades at the next close.
- A stamp that the job did not write is refused: the tests fail on the pull request, and the job stops if it is merged anyway. A typed stamp could buy a call at a close that is already known.
- `forced_exit` (a date) closes a call early when its ticker stops trading. Leave it empty otherwise. See "When the job stops" below.
- `status = excluded` keeps a call on record without trading it.
- Never edit the legs or dates of a call that has already entered. Published days cannot change, and the run fails if they would.

A disclosed sale on air does not close a call: the rule is 12 months. Record it in `note`.

## Screening new episodes

A scheduled Claude session reads new episodes twice a day and opens a pull request with any call it finds. `docs/screening.md` is the procedure it follows and is yours to edit. Merging the pull request is the trade.

- Transcripts come from a third-party site and appear one to four days after an episode, so a call usually reaches the book a few days after it was made.
- An episode with no transcript after five days is read from summaries. A call seen only in a summary is listed for your decision and not added.

## How it runs

`.github/workflows/paper.yml` runs at 01:07 UTC from Tuesday to Saturday, with a second slot at 07:07, and whenever `calls.csv` or `strategies.toml` changes on `main`. It stamps new calls, downloads daily bars from Yahoo Finance for every ticker still held, appends only bars that are final, recomputes everything from the ledger and the stored prices, and commits `data/` and `results/`.

`.github/workflows/episodes.yml` runs every four hours and appends newly published episodes to `data/episodes.csv`.

- A day is published only when every US symbol and cryptocurrency held that day has its final price for it.
- A stored price is never replaced, and a price for a day that is already published is never added. Each new level is the one that most of the last seven stored bars imply through the vendor's adjusted closes, so nothing the data vendor does later can move a published day.
- A dividend the vendor books late is picked up by the next bar, whatever its size, as long as most of the last seven stored bars are older than the ex-date: up to three sessions late. Booked later than that, it is lost, and the series understates the return by that dividend. Yahoo normally books on the ex-date.
- A US symbol's bar is stored only on days the New York Stock Exchange is open by its standing rules. An unscheduled closure is not known to the code: a bar the vendor showed for one would become a session with no move.
- If a held US ticker has no bar for one session and trades again the next, that session is published with the ticker at its previous close and the two-day move lands on the next day.
- Samsung and SK Hynix are held as dollar series built from the Seoul listings and the won-dollar rate. Over a Seoul holiday they are carried at their last close; the run prints a warning when a carried price is more than four days old.
- A won-dollar rate more than 10% away from the two before it is taken for a bad print. That day's bar is left out with a warning and the shares are carried at their last close; the next good bar brings the level up to date. Nothing stops.
- A call that loses its whole stake (a short that more than doubles) is closed at nothing that day.
- The paper book starts in cash on `paper_start`, buys the calls that are open at that close in equal parts, and sells each on its original exit day.

### When the job stops

The job fails on purpose instead of publishing something doubtful. GitHub emails you when it does.

| Message | What happened | What to do |
|---|---|---|
| `N sessions cannot be published: since <day> there is no price for <ticker>` | The ticker stopped trading (takeover, delisting) or the vendor dropped it. | Put that day in the call's `forced_exit`. The call is closed that day at its last price. |
| `no price history for <ticker>` | The vendor no longer serves the symbol, or could not be reached. | If it keeps failing for one ticker only, the symbol is gone: give its call a `forced_exit` on the first unpublished day. If every ticker fails, the vendor is blocking the runner: wait, the next run tries again. |
| `the bar of <day> is X times the one before it` | A one-day move beyond half or double: a split shown unadjusted, a bad print, or a real collapse. | Check the price. If it is real, run the workflow by hand with the ticker in `allow_jump`. If not, wait: the next run tries again. |
| `the last stored bars and the download disagree ... no majority agrees` | The vendor's history for the ticker no longer matches what is stored, and not in the way a late dividend would: several changes at once, or the symbol now means something else. | Check the symbol. If the vendor is right, run the workflow by hand with the ticker in `allow_jump`. If the symbol was reused, give the call a `forced_exit`. |
| `none of the last 7 stored days is in the download` | The vendor's history starts after the last stored bar, so the series cannot be continued. | If the ticker's call is over or has not started, run by hand with the ticker in `allow_jump`: the series starts again at the vendor's level. If it is held, give its call a `forced_exit`. |
| `its recorded_utc was not written by the scheduled job` | A stamp was typed into `calls.csv`, or changed afterwards. | Empty the field. The job stamps the call at its next run. |
| `published days would change` | A line of `calls.csv` was edited after its call entered. | Undo the edit. Only if the change is intended, run the workflow by hand with `allow_restate` set to `yes`. |

A ticker confirmed with `allow_jump` is confirmed once. The job writes the decision to `data/price_trust.csv`, and from then on only the bars stored since that run decide the ticker's next level, so the scheduled runs after it need nothing from you.

```
pip install -e ".[dev]"
pytest -q                         # 71 tests
python -m allin.cli status        # the book as it stands: what is held, since when, until when
python -m allin.cli run           # recompute and write ./results from stored prices
python -m allin.cli run --fetch   # the same after appending the latest final prices
python -m allin.cli episodes      # append newly published episodes to the catalogue
```

## What has and has not been checked

- The portfolio engine is tested against a second, independent implementation and against the recovered figures (24,634 USD on 25 Sep 2026 without GEODNET; the original reported 25,008 with it).
- The live price download has been tested with a stand-in for the data vendor, not against Yahoo itself: the build environment could not reach it. The first scheduled run is the real test, and it fails loudly rather than publish a partial day.
- The command the job runs has been rehearsed over six simulated weeks with that stand-in: new calls stamped by the job, exchange holidays, a dividend booked late, a foreign bar posted late, an outage, a ticker that stopped trading and a fall of more than half. Each session was published once and no published row changed.
- The exchange calendar in the code matches every SPY session from January 2021 to November 2026, apart from the one unscheduled closure in that time (9 Jan 2025).
- The same holds for the episode feed: the parser is tested on a sample document, not on the publisher's live feed.
- The frozen price snapshot was assembled from public GitHub copies of Yahoo data. Every US-listed ticker held since 25 Sep 2026 was cross-checked against a second copy.
- The calls dated after 30 Sep 2026 were read from machine-made transcripts by a language model. Three episodes in that stretch (28 Sep, 30 Sep, 2 Oct) could only be read as third-party summaries.
- `reproduce_strategy.MD` is the earlier handoff note kept for the record; it holds the episode catalogue and no strategy results.
