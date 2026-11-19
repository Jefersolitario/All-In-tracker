"""The call ledger, and the calendar rule that turns a published episode into an entry and an exit.

Rule (recovered by matching 40 of the original 45 call returns to the decimal):
    entry = close of the first US session after the episode's publication date in New York time
    exit  = close one calendar year after the entry, rolled forward to a session
A call written into the ledger after its episode came out enters after it was recorded instead (`recorded_utc`),
so nothing is ever bought at a price that was already in the past when the call was written down.
"""
from __future__ import annotations

import bisect
import csv
import dataclasses
import datetime as dt
import math
import pathlib
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
COLUMNS = ("call_id", "episode_guid", "published_utc", "speaker", "call", "legs", "status", "recorded_utc", "recovered_return_pct", "forced_exit", "note")
HOSTS = ("Chamath", "Sacks", "Jason", "Friedberg")


@dataclasses.dataclass(frozen=True)
class Call:
    call_id: int
    episode_guid: str
    published_utc: dt.datetime
    speaker: str
    call: str
    legs: tuple[tuple[str, float], ...]      # (ticker, signed weight); absolute weights sum to 1
    status: str                              # "active" is traded; "excluded" is kept for the record only
    recorded_utc: dt.datetime | None         # when the call was written into this file; blank for the recovered backtest
    recovered_return_pct: float | None       # the figure in the recovered backtest, where there is one
    forced_exit: dt.date | None              # close early on this day (delisting, takeover); blank otherwise
    note: str

    @property
    def host_only(self) -> bool:
        names = [n.strip() for n in self.speaker.replace(";", "/").split("/")]
        return all(n in HOSTS for n in names)


def _ts(text: str) -> dt.datetime | None:
    text = (text or "").strip()
    if not text:
        return None
    return dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def parse_legs(text: str) -> tuple[tuple[str, float], ...]:
    legs = []
    for part in text.split(";"):
        ticker, _, weight = part.strip().rpartition(":")
        if not ticker:
            raise ValueError(f"leg without a weight: {part!r}")
        legs.append((ticker.strip(), float(weight)))
        if not math.isfinite(legs[-1][1]) or legs[-1][1] == 0:
            raise ValueError(f"leg weight must be a non-zero number: {part!r}")
    total = sum(abs(w) for _, w in legs)
    if not legs or abs(total - 1) > 1e-3:
        raise ValueError(f"absolute leg weights must sum to 1, got {total:.4f} in {text!r}")
    return tuple(legs)


def load_calls(path: str | pathlib.Path) -> list[Call]:
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    if not rows or tuple(rows[0]) != COLUMNS:
        raise ValueError(f"calls file must have the columns {COLUMNS}")
    calls = []
    for n, row in enumerate(rows[1:], start=2):
        if not any(x.strip() for x in row):
            continue                                                  # a blank line
        if len(row) != len(COLUMNS):
            raise ValueError(f"calls file line {n}: expected {len(COLUMNS)} fields, found {len(row)} (a comma inside a field needs double quotes)")
        r = dict(zip(COLUMNS, row))
        try:
            call_id = int(r["call_id"])
        except ValueError:
            raise ValueError(f"calls file line {n}: call_id {r['call_id']!r} is not a whole number") from None
        if r["status"] not in ("active", "excluded"):
            raise ValueError(f"call {call_id}: status must be active or excluded")
        published = _ts(r["published_utc"])
        if published is None:
            raise ValueError(f"call {call_id}: published_utc is required")
        rec = r["recovered_return_pct"].strip(); forced = r["forced_exit"].strip()
        calls.append(Call(call_id, r["episode_guid"], published, r["speaker"], r["call"], parse_legs(r["legs"]), r["status"],
                          _ts(r["recorded_utc"]), float(rec) if rec else None, dt.date.fromisoformat(forced) if forced else None, r["note"]))
    ids = [c.call_id for c in calls]
    if len(set(ids)) != len(ids):
        raise ValueError("call ids must be unique")
    return sorted(calls, key=lambda c: (c.published_utc, c.call_id))


def field_spans(line: str) -> list[tuple[int, int]]:
    """Start and end of every comma-separated field in one line of CSV (without its line ending), honouring double quotes."""
    spans, start, quoted, i = [], 0, False, 0
    while i < len(line):
        ch = line[i]
        if ch == '"':
            if quoted and line[i + 1:i + 2] == '"':
                i += 1                                                # an escaped quote inside a quoted field
            else:
                quoted = not quoted
        elif ch == "," and not quoted:
            spans.append((start, i)); start = i + 1
        i += 1
    if quoted:
        raise ValueError("a quoted field is not closed on its line; keep each call on one line")
    spans.append((start, len(line)))
    return spans


def plus_year(d: dt.date) -> dt.date:
    try:
        return d.replace(year=d.year + 1)
    except ValueError:          # 29 February
        return d.replace(year=d.year + 1, day=28)


def entry_date(call: Call, sessions: list[dt.date]) -> dt.date | None:
    """First session after the New York date on which the call was published (and recorded, if that was later)."""
    after = call.published_utc.astimezone(NY).date()
    if call.recorded_utc is not None:
        after = max(after, call.recorded_utc.astimezone(NY).date())
    i = bisect.bisect_right(sessions, after)
    return sessions[i] if i < len(sessions) else None


def exit_date(entry: dt.date, sessions: list[dt.date], forced: dt.date | None = None) -> dt.date | None:
    """First session on or after the first anniversary of the entry, or on or after `forced` if that comes first.
    None while that day is still beyond the stored sessions."""
    target = plus_year(entry) if forced is None else min(plus_year(entry), forced)
    i = bisect.bisect_left(sessions, target)
    return sessions[i] if i < len(sessions) else None
