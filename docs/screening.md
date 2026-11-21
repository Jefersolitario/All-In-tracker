# Screening new episodes for calls

A scheduled Claude session follows this page twice a day. It reads the episodes that have not been screened yet, writes any investment call into `calls.csv` on a branch, and opens one pull request. Nothing is traded until the owner merges: the daily job stamps a new call with the time it reached `main`, and the call is bought at the close of the first US session after that New York date.

## 1. Which episodes

- `data/episodes.csv` lists every published episode with its release time. A workflow refreshes it from the publisher's feed every four hours.
- `data/screening_log.csv` has one line for every episode already read.
- **To do:** every episode in the catalogue published on or after `screening_start` (in `strategies.toml`) that has no line in the log, oldest first.
- **To re-read:** a log line with `read_from = summary` whose episode is less than 14 days old, if a transcript has appeared since.

## 2. Where to read

1. **Transcript.** Open the show index at https://podcasts.happyscribe.com/all-in-with-chamath-jason-sacks-friedberg (older entries are on `?page=2`), find the episode by title and open its transcript page. Take the link from the index; the addresses cannot be guessed from the title.
2. **Not there yet.** Transcripts appear one to four days after an episode. If the episode is less than 5 days old, leave it for the next run and write nothing in the log.
3. **Still not there after 5 days.** Search the web for summaries of the episode and read at least two independent ones. Log the episode with `read_from = summary`. A call seen only in summaries is never added: list it under "Needs your decision" in the pull request.

The reading tool returns about 100,000 characters of a page per request. A weekly show is longer than that, so keep reading with the offset the tool reports until the page ends. An episode is not screened until its last part has been read.

## 3. How to read

**First pass, every part of the page.** Ask for every statement in which a speaker says they bought, own, are buying, would buy, sold or are short a public stock, fund, commodity or cryptocurrency, or recommends getting long or short an asset, a sector, a theme, a country or an asset class. For each: the time in the episode, the speaker (and whether that is certain), the asset, the direction, own position or advice, and a one-sentence paraphrase.

**Second pass, every candidate.** Go back to the passage and settle: who is speaking and how you can tell (the transcript often has no speaker labels: look for a name used by the others, or who moderates); which asset exactly and whether a ticker or company is named; whether it is the speaker's own money or advice; when the purchase happened; anything said in the same breath that takes it back.

Write paraphrases. Do not copy transcript text into this repository.

## 4. What counts as a call

Add a call when a host or a guest:

- recommends buying, selling or shorting a listed asset, a sector or a theme;
- says they bought or sold short recently ("this week", "during the show", "through the sell-off");
- states a position they hold now together with a view on where it is going.

Do not add:

- private companies, and anything with no listed instrument;
- a chief executive or founder talking about their own company;
- a forecast with no position and no recommendation;
- a past trade told as a story, with no current view;
- a view the speaker takes back ("I'm not telling anyone to buy");
- rhetoric, jokes, hypotheticals and sponsor reads.

A borderline statement goes under "Needs your decision" with the reason, not into the ledger.

A sale or an exit announced on air is not a call and does not close anything: the rule is 12 months. Append one sentence about it to the `note` of the call it refers to.

## 5. Which instrument

| The speaker names | Use |
|---|---|
| A US-listed stock or fund | Its Yahoo Finance symbol. Alphabet is `GOOG`. |
| A foreign company with a US-listed share or ADR | The US symbol |
| A foreign company with no US listing | Do not add; "Needs your decision" |
| Bitcoin, another cryptocurrency | `BTC-USD`, `<SYMBOL>-USD` |
| Semiconductors | `SMH` |
| Electricity, power, utilities | `XLU` |
| Energy meaning oil and gas | `XLE` |
| Gold, copper, uranium | `GLD`, `CPER`, `URA` |
| US stocks, "the market" | `SPY` |
| Large technology stocks, the Nasdaq | `QQQ` |
| Long-dated Treasuries | `TLT` |
| Software, defence, biotech | `IGV`, `ITA`, `XBI` |
| Any other theme | Do not add; "Needs your decision" |

- Several assets named in one statement by one speaker are one call with equal weights.
- A short is a negative weight (`IONQ:-1`).
- A symbol the price store has never held is allowed: the daily job downloads its history. Say so in the pull request so the owner can check the symbol.

## 6. What to write

**`calls.csv`**, one line per call, appended at the end:

| Column | Value |
|---|---|
| `call_id` | The next integer |
| `episode_guid`, `published_utc` | Copied from `data/episodes.csv` |
| `speaker` | A host by first name exactly as `Chamath`, `Sacks`, `Jason` or `Friedberg`; a guest by full name; two speakers as `Jason / Sacks` |
| `call` | A few words, for example `Uranium miners` |
| `legs` | `TICKER:weight` pairs separated by `;`, absolute weights summing to 1 |
| `status` | `active` |
| `recorded_utc` | **Leave empty.** The daily job fills it in when the line reaches `main`. A line that arrives with it filled in fails the tests on the pull request. |
| `recovered_return_pct`, `forced_exit` | Empty |
| `note` | Where in the episode, a one-sentence paraphrase, the proxy if one was used, and how sure the speaker is |

**`data/screening_log.csv`**, one line per episode read, with or without a call: `guid`, `published_utc`, `title` from the catalogue; `read_from` (`transcript` or `summary`); `transcript_url`; `screened_utc` (now); `calls_added` (ids separated by `;`); `note` (what was left out and why).

Then run `pytest -q`. It must pass before anything is pushed.

## 7. Branch and pull request

Which branch to work on:

1. If no branch named `claude/screening` (or `claude/screening-<date>`) exists on the remote, start `claude/screening` from `main`.
2. If one exists, check it out and merge `main` into it, so that it sees the latest catalogue. A clean merge, or a fast-forward, means continue on it.
3. If that merge conflicts, abort it. Then, if every call and every log line on the branch is already on `main`, the branch was merged in a way that rewrote it: leave it alone, start a new branch `claude/screening-<today's date>` from `main`, and say in the summary that the old branch can be deleted. Otherwise stop and report the conflict.

Always read the ledger and the log from the branch you ended up on, not from `main`: it may hold lines that are not merged yet.

What to push:

- A run that only adds log lines pushes the branch and opens nothing.
- A run that adds a call, or has something for "Needs your decision", opens one pull request into `main`, or updates the description of the open one. The description lists: the calls added (episode, speaker, call, instrument, where in the episode, confidence); what needs a decision; the episodes read with no call; the episodes still waiting for a transcript. It ends with: "Merging this trades the calls: each is bought at the close of the first US session after the merge (New York date)."
- Never push to `main`, never merge, never force-push, never delete a branch, never fill in `recorded_utc`, and never change the legs or dates of a call that already has a stamp.

## 8. For the owner

- Merging the pull request is the trade. A call merged before midnight New York time is bought at the next session's close.
- To reject one call, delete its line in the pull request before merging, or set its `status` to `excluded`.
- The criteria and the proxy table on this page are yours to edit; the scheduled session reads the version on `main`.
