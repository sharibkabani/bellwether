from bellwether.news import Headline, NewsFeed

_RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Test Feed</title>
  <item><title>Bitcoin ETF sees record inflows</title>
        <pubDate>Mon, 02 Jun 2025 12:00:00 GMT</pubDate></item>
  <item><title>Solana network upgrade goes live</title>
        <pubDate>Mon, 02 Jun 2025 09:00:00 GMT</pubDate></item>
  <item><title><![CDATA[Ethereum staking hits new high]]></title>
        <pubDate>Sun, 01 Jun 2025 18:00:00 GMT</pubDate></item>
</channel></rss>"""

_ATOM = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><title>XRP ruling appealed</title>
         <updated>2025-06-02T10:00:00Z</updated></entry>
</feed>"""


def test_parses_rss_items():
    items = NewsFeed._parse(_RSS, "https://www.coindesk.com/rss")
    titles = [h.title for h in items]
    assert "Bitcoin ETF sees record inflows" in titles
    assert "Ethereum staking hits new high" in titles  # CDATA handled
    assert all(h.source == "coindesk.com" for h in items)
    assert any(h.published is not None for h in items)


def test_parses_atom_entries():
    items = NewsFeed._parse(_ATOM, "https://decrypt.co/feed")
    assert len(items) == 1
    assert items[0].title == "XRP ruling appealed"
    assert items[0].published is not None


def test_parse_handles_garbage():
    assert NewsFeed._parse(b"not xml at all", "http://x") == []


def test_relevant_matches_name_and_symbol():
    heads = [
        Headline("Bitcoin ETF sees record inflows", "x"),
        Headline("Solana network upgrade goes live", "x"),
        Headline("Some unrelated stock news", "x"),
        Headline("BTC dominance climbs", "x"),
    ]
    btc = NewsFeed.relevant(heads, "BTC", "Bitcoin", limit=5)
    btc_titles = [h.title for h in btc]
    assert "Bitcoin ETF sees record inflows" in btc_titles  # matched by name
    assert "BTC dominance climbs" in btc_titles             # matched by symbol
    assert "Some unrelated stock news" not in btc_titles

    sol = NewsFeed.relevant(heads, "SOL", "Solana", limit=5)
    assert len(sol) == 1 and "Solana" in sol[0].title


def test_relevant_respects_limit():
    heads = [Headline(f"Bitcoin update {i}", "x") for i in range(10)]
    assert len(NewsFeed.relevant(heads, "BTC", "Bitcoin", limit=3)) == 3
