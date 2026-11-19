"""The episode catalogue: every episode the show has published, with the release time from the publisher's feed.

The catalogue is what tells the screening job that an episode exists before any transcript of it does.
Rows are only ever appended: an episode keeps the title and time it had when it was first seen.
"""
from __future__ import annotations

import csv
import datetime as dt
import pathlib
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

FEED = "https://allinchamathjason.libsyn.com/rss"
COLUMNS = ("published_utc", "guid", "title", "episode_url", "audio_url")


def fetch_feed(url: str = FEED) -> bytes:
    import requests
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; all-in-tracker catalogue)"}, timeout=60)
    r.raise_for_status()
    return r.content


def parse_feed(xml: bytes) -> list[tuple[str, str, str, str, str]]:
    """Catalogue rows from an RSS document, oldest first."""
    lowered = xml.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise ValueError("the feed declares a document type or entities; refusing to parse it")
    rows = []
    for item in ET.fromstring(xml).findall("./channel/item"):
        guid = (item.findtext("guid") or "").strip()
        title = " ".join((item.findtext("title") or "").split())
        stamp = (item.findtext("pubDate") or "").strip()
        if not (guid and title and stamp):
            raise ValueError("an episode in the feed is missing its identity, title or timestamp")
        when = parsedate_to_datetime(stamp)
        if when.tzinfo is None:
            raise ValueError(f"episode {guid}: the release time has no time zone")
        enclosure = item.find("enclosure")
        rows.append((when.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), guid, title, (item.findtext("link") or "").strip(),
                     enclosure.attrib.get("url", "") if enclosure is not None else ""))
    if not rows or len({r[1] for r in rows}) != len(rows):
        raise ValueError("empty feed or duplicate episode identity")
    return sorted(rows)


def update_catalogue(path: str | pathlib.Path, feed_rows: list[tuple[str, str, str, str, str]]) -> list[tuple[str, str, str, str, str]]:
    """Append the episodes the catalogue does not know yet. Returns the rows added."""
    with open(path, newline="", encoding="utf-8") as f:
        stored = list(csv.reader(f))
    if not stored or tuple(stored[0]) != COLUMNS:
        raise ValueError(f"the catalogue must have the columns {COLUMNS}")
    known = {r[1] for r in stored[1:]}
    new = [r for r in feed_rows if r[1] not in known]
    if new:
        with open(path, "a", newline="", encoding="utf-8") as f:
            csv.writer(f, lineterminator="\n").writerows(new)
    return new


def load_catalogue(path: str | pathlib.Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))
