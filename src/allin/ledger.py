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
        legs.append((ticker, float(weight)))
    total = sum(abs(w) for _, w in legs)
    if not legs or abs(total - 1) > 1e-3:
        raise ValueError(f"absolute leg weights must sum to 1, got {total:.4f} in {text!r}")
    return tuple(legs)


def load_calls(path: str | pathlib.Path) -> list[Call]:
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if rows and tuple(rows[0].keys()) != COLUMNS:
        raise ValueError(f"calls file must have the columns {COLUMNS}")
    calls = []
    for r in rows:
        if r["status"] not in ("active", "excluded"):
            raise ValueError(f"call {r['call_id']}: status must be active or excluded")
        published = _ts(r["published_utc"])
        if published is None:
            raise ValueError(f"call {r['call_id']}: published_utc is required")
        rec = r["recovered_return_pct"].strip(); forced = r["forced_exit"].strip()
        calls.append(Call(int(r["call_id"]), r["episode_guid"], published, r["speaker"], r["call"], parse_legs(r["legs"]), r["status"],
                          _ts(r["recorded_utc"]), float(rec) if rec else None, dt.date.fromisoformat(forced) if forced else None, r["note"]))
    ids = [c.call_id for c in calls]
    if len(set(ids)) != len(ids):
        raise ValueError("call ids must be unique")
    return sorted(calls, key=lambda c: (c.published_utc, c.call_id))


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
