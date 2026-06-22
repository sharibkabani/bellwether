"""Free crypto news feed for grounding the AI signal in current events.

Pulls recent headlines from public RSS feeds (CoinDesk, Cointelegraph, Decrypt
by default) — no API key, no cost, no usage terms to worry about. Parsing uses
the standard library (``xml.etree``), so there's no extra dependency.

The AI trending signal injects these headlines into its prompt so "what's
trending" reflects *today's* news rather than the model's training cutoff. The
feed fails soft: any feed that errors is skipped, and if all fail the signal
still runs on price + model knowledge.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

import requests

DEFAULT_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
]

_UTC = _dt.timezone.utc
_EPOCH = _dt.datetime.min.replace(tzinfo=_UTC)


@dataclass
class Headline:
    title: str
    source: str
    published: _dt.datetime | None = None


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _local(tag: str) -> str:
    """Strip an XML namespace, e.g. '{http://www.w3.org/2005/Atom}title' -> 'title'."""
    return tag.rsplit("}", 1)[-1]


def _parse_date(s: str | None) -> _dt.datetime | None:
    if not s:
        return None
    s = s.strip()
    for parse in (parsedate_to_datetime, lambda x: _dt.datetime.fromisoformat(x.replace("Z", "+00:00"))):
        try:
            dt = parse(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_UTC)
            return dt
        except (TypeError, ValueError):
            continue
    return None


class NewsFeed:
    def __init__(self, feeds: list[str] | None = None, max_headlines: int = 40, timeout: int = 10):
        self._feeds = feeds or DEFAULT_FEEDS
        self._max = max_headlines
        self._timeout = timeout

    def headlines(self) -> list[Headline]:
        """Fetch, parse, dedupe, and sort all feeds (newest first)."""
        collected: list[Headline] = []
        for url in self._feeds:
            try:
                resp = requests.get(
                    url, timeout=self._timeout, headers={"User-Agent": "bellwether/0.1"}
                )
                resp.raise_for_status()
                collected.extend(self._parse(resp.content, url))
            except Exception:
                continue  # fail soft per feed

        collected.sort(key=lambda h: h.published or _EPOCH, reverse=True)
        seen: set[str] = set()
        deduped: list[Headline] = []
        for h in collected:
            key = h.title.lower()
            if key and key not in seen:
                seen.add(key)
                deduped.append(h)
        return deduped[: self._max]

    @staticmethod
    def _parse(content: bytes, url: str) -> list[Headline]:
        source = re.sub(r"^www\.", "", re.sub(r"https?://", "", url).split("/")[0])
        try:
            root = ET.fromstring(content)
        except ET.ParseError:
            return []
        items: list[Headline] = []
        for el in root.iter():
            if _local(el.tag) not in ("item", "entry"):
                continue
            title = None
            published = None
            for child in el:
                tag = _local(child.tag)
                if tag == "title" and title is None:
                    title = _clean(child.text or "".join(child.itertext()))
                elif tag in ("pubDate", "published", "updated") and published is None:
                    published = _parse_date(child.text)
            if title:
                items.append(Headline(title=title, source=source, published=published))
        return items

    @staticmethod
    def relevant(headlines: list[Headline], symbol: str, name: str, limit: int = 3) -> list[Headline]:
        """Headlines that mention a coin by name or ticker (word-boundary)."""
        name_l = (name or "").lower()
        sym_re = re.compile(rf"\b{re.escape(symbol.lower())}\b")
        out: list[Headline] = []
        for h in headlines:
            t = h.title.lower()
            if (name_l and name_l in t) or sym_re.search(t):
                out.append(h)
                if len(out) >= limit:
                    break
        return out
