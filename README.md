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
| `data/venue_symbols.csv` | Where each ticker trades around the clock, for the second strategy. Yours to edit. |
| `data/venue_entries.csv` | One line per leg bought early: venue, times and prices. Lines up to call 49 are the backtest; later ones are written by the job, once. |
| `data/price_trust.csv` | Appears the first time you confirm a ticker's data by hand: which ticker, from which day. Only the job writes it. |
| `data/episodes.csv` | Catalogue of every episode with its release time. A workflow appends new ones from the publisher's feed. |
| `data/screening_log.csv` | One line per episode read for calls since 26 Sep 2026: what it was read from and what was found. |
| `docs/screening.md` | How new episodes are screened: what counts as a call, which instrument, what gets written. |
| `data/tbill_3m.csv` | Three-month Treasury bill yield (FRED series DTB3), for the Sharpe over bills. |
| `src/allin/` | `ledger` (calendar rule), `prices` (store and live update), `engine` (portfolio), `episodes` (catalogue), `venue` (buys on a 24/7 venue), `cli` (publisher), `contract` (results format). |
| `results/` | Published output: `index.json`, `backtest_summary.parquet`, and for each published strategy `daily.parquet`, `manifest.json`, `positions.csv`. |

## The second strategy: bought on a 24/7 venue

The dashboard shows two strategies: **All-In calls: at the close** (id `allin_12m`) and **All-In calls: 24/7** (id `allin_247`). The 24/7 one holds the same book as the first and sells on the same days. The one difference is when a new call is bought.

- **When:** at the first hourly open after the job stamped the call, on a venue that trades around the clock. "At the close" waits for the close of the next US session.
- **Where:** `data/venue_symbols.csv` names the venue and symbol for each ticker: Hyperliquid's stock and commodity contracts, Binance for cryptocurrencies. The file is yours to edit. A ticker that is not in it, or has no candle within six hours of the buy or of the close, waits for the close like in the first strategy.
- **What it is worth:** the venue's own price when the rule buys, divided by its price at the early buy. The call gets the same dollars as in the first strategy, times that ratio, and the difference is booked on the entry day. From then on both strategies hold the same thing and are valued with the same official closes.
- **The record:** each buy is written once to `data/venue_entries.csv` by the job: the two times, the two prices and the venue. The times follow from the stamp and the calendar, and the run refuses a line whose times do not. The prices are the venue's public hourly candles, so a line can be checked afterwards.
- **The comparison moment** is the New York close for US symbols (16:00, also on half days), midnight UTC for cryptocurrencies and the Seoul close for the two Seoul shares.
- **Not counted:** fees, spread and funding on the venue, and what it would cost to hold the position there for 12 months or to move it into shares.

In the backtest, the 19 calls that had a venue market at the time are bought at the first candle opening at least one hour after their episode. That takes the value on 25 Sep 2026 from 24,634 to 26,108 USD. Most of the difference is two cryptocurrency calls in 2021, when the book held two to four positions. A review of that backtest found no reliable evidence of an edge: 11 of 19 calls did better bought early, and without two SpaceX calls the stocks average +0.1%. The strategy is published with role `monitor` to find out on paper.

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
- A held ticker that has no bar for the latest session is named in a warning on the run from the first time. The job stops only once more than three sessions are stuck behind it.
- Samsung and SK Hynix are held as dollar series built from the Seoul listings and the won-dollar rate. Over a Seoul holiday they are carried at their last close; the run prints a warning when a carried price is more than four days old.
- A won-dollar rate more than 10% away from the two before it is taken for a bad print. That day's bar is left out with a warning and the shares are carried at their last close; the next good bar brings the level up to date. Nothing stops.
- A call that loses its whole stake (a short that more than doubles) is closed at nothing that day.
- The paper book starts in cash on `paper_start`, buys the calls that are open at that close in equal parts, and sells each on its original exit day.

### When the job stops

The job fails on purpose instead of publishing something doubtful. GitHub emails you when it does.

| Message | What happened | What to do |
|---|---|---|
| `N sessions cannot be published: since <day> there is no price for <ticker>` | The vendor has had no bar for the ticker for more than three sessions. Either it stopped trading (takeover, delisting), or it still trades and the vendor's history has a gap (Sandisk after its split, November 2026). | Check which. Stopped trading: put that day in the call's `forced_exit`; the call is closed that day at its last price. Still trading: do not close the call, add the missing closes by hand as described under "A ticker that still trades but gets no bars". |
| `no price history for <ticker>` | The vendor no longer serves the symbol, or could not be reached. | If it keeps failing for one ticker only, the symbol is gone: give its call a `forced_exit` on the first unpublished day. If every ticker fails, the vendor is blocking the runner: wait, the next run tries again. |
| `the bar of <day> is X times the one before it` | A one-day move beyond half or double: a split shown unadjusted, a bad print, or a real collapse. | Check the price. If it is real, run the workflow by hand with the ticker in `allow_jump`. If not, wait: the next run tries again. |
| `the last stored bars and the download disagree ... no majority agrees` | The vendor's history for the ticker no longer matches what is stored, and not in the way a late dividend would: several changes at once, or the symbol now means something else. | Check the symbol. If the vendor is right, run the workflow by hand with the ticker in `allow_jump`. If the symbol was reused, give the call a `forced_exit`. |
| `none of the last 7 stored days is in the download` | The vendor's history starts after the last stored bar, so the series cannot be continued. | If the ticker's call is over or has not started, run by hand with the ticker in `allow_jump`: the series starts again at the vendor's level. If it is held, give its call a `forced_exit`. |
| `venue: cannot download <symbol> from <venue> for call N` | The 24/7 venue could not be reached. Nothing was written, for either strategy. | Wait: the next run tries again. To give up on buying that call early, add one line per leg to `data/venue_entries.csv` with venue `none` and no times or prices. |
| `venue_entries.csv, call N <ticker>: bought at ..., which is not an hourly open in the six hours from ...` | A line was typed or changed by hand. | Restore the line the job wrote. |
| `its recorded_utc was not written by the scheduled job` | A stamp was typed into `calls.csv`, or changed afterwards. | Empty the field. The job stamps the call at its next run. |
| `published days would change` | A line of `calls.csv` was edited after its call entered. | Undo the edit. Only if the change is intended, run the workflow by hand with `allow_restate` set to `yes`. |

### A ticker that still trades but gets no bars

Seen once: Sandisk split 8-for-1 after the close of 19 Nov 2026 and Yahoo's daily history for `SNDK` stopped on that day, while the share kept trading and Yahoo's own quote page kept showing its closes. The job cannot tell this from a delisting. From the first run without a bar it prints a warning on the run (`SNDK has no bar for <day>`), and after more than three such sessions it stops.

A `forced_exit` would be wrong here: it closes the whole call, the other legs included. Add the missing closes instead:

1. Take the official close of each missing session from two sources that agree, on one split basis for all of them.
2. For each day append `date,ticker,level` to `data/prices_live.csv`, where level = the last level stored for the ticker × (that day's close ÷ the close of the last stored day, on the same split basis). If a dividend went ex in between, add it to the close.
3. Check the result: each new level over the one before it must be that day's real percentage move. A raw post-split price typed in as a level would be stored as a fall of most of the stake, and nothing in the job checks a row typed by hand.
4. Only days that are not published yet. Note the rows and where the closes came from in `data/SOURCES.md`.

The next run publishes up to the last day added. When the vendor's history comes back the job carries on from the stored levels by itself. Until then the ticker needs another row before more than three sessions pile up again.

This is a bridge for about a week, not a second source. The job downloads from 12 days before the ticker's last stored day and needs at least one of the vendor's bars in that window. Once the rows typed in reach more than 12 days past the vendor's last bar, the run stops with `no price history for <ticker>`. A gap that lasts that long needs another source for the ticker.

A ticker confirmed with `allow_jump` is confirmed once. The job writes the decision to `data/price_trust.csv`, and from then on only the bars stored since that run decide the ticker's next level, so the scheduled runs after it need nothing from you.

```
pip install -e ".[dev]"
pytest -q                         # 82 tests
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
