"""Render the static blog site (index + one page per day).

No templating engine or build step — just writes HTML files plus a small
``posts.json`` manifest so the index can list every post without re-parsing
them. A ``.nojekyll`` marker tells GitHub Pages to serve the files as-is.
"""

from __future__ import annotations

import datetime as _dt
import html
import json
import os

from .generator import BlogPost

_CSS = """\
:root { --bg:#0d1117; --card:#161b22; --fg:#e6edf3; --muted:#8b949e;
        --up:#3fb950; --down:#f85149; --accent:#58a6ff; --border:#30363d; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
       font:16px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif; }
.wrap { max-width:720px; margin:0 auto; padding:32px 20px 64px; }
header a { color:var(--fg); text-decoration:none; font-weight:700; font-size:20px; }
header .tag { color:var(--muted); font-weight:400; font-size:14px; }
h1 { font-size:28px; line-height:1.25; margin:24px 0 4px; }
h2 { font-size:20px; margin:28px 0 8px; }
h3 { font-size:17px; margin:22px 0 6px; }
a { color:var(--accent); }
.muted { color:var(--muted); font-size:14px; }
.pct-up { color:var(--up); font-weight:600; }
.pct-down { color:var(--down); font-weight:600; }
ul { padding-left:22px; }
code { background:#1f2630; padding:2px 5px; border-radius:5px; font-size:14px; }
.post-card { display:block; background:var(--card); border:1px solid var(--border);
             border-radius:12px; padding:18px 20px; margin:14px 0; text-decoration:none; color:inherit; }
.post-card:hover { border-color:var(--accent); }
.post-card h2 { margin:0 0 4px; font-size:19px; }
.post-card p { margin:8px 0 0; color:var(--muted); }
footer { margin-top:48px; padding-top:20px; border-top:1px solid var(--border);
         color:var(--muted); font-size:13px; }
.article { background:var(--card); border:1px solid var(--border); border-radius:12px; padding:8px 28px 28px; }
"""

_DISCLAIMER = (
    "Bellwether is an autonomous research bot. This journal is for educational "
    "purposes and is <strong>not financial advice</strong>. Past (and simulated) "
    "performance does not predict future results."
)


def _pct_span(pct: float) -> str:
    cls = "pct-up" if pct >= 0 else "pct-down"
    return f'<span class="{cls}">{pct:+.2f}%</span>'


class SiteBuilder:
    def __init__(self, out_dir: str, title: str, base_url: str = ""):
        self._dir = out_dir
        self._title = title
        self._base_url = base_url.rstrip("/")
        os.makedirs(os.path.join(out_dir, "posts"), exist_ok=True)

    def _manifest_path(self) -> str:
        return os.path.join(self._dir, "posts.json")

    def _load_manifest(self) -> list[dict]:
        try:
            with open(self._manifest_path()) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return []

    def add_post(self, post: BlogPost) -> str:
        """Write/replace the post page, update the manifest, regenerate index.
        Returns the relative path of the post page."""
        # Write the article page.
        post_rel = f"posts/{post.date}.html"
        with open(os.path.join(self._dir, post_rel), "w") as f:
            f.write(self._post_page(post))

        # Upsert manifest by date (re-running the same day overwrites).
        manifest = [m for m in self._load_manifest() if m.get("date") != post.date]
        manifest.append({
            "date": post.date,
            "title": post.title,
            "summary": post.summary,
            "day_pct": post.day_pct,
            "total_pct": post.total_pct,
            "file": post_rel,
        })
        manifest.sort(key=lambda m: m.get("date", ""), reverse=True)
        with open(self._manifest_path(), "w") as f:
            json.dump(manifest, f, indent=2)

        # Regenerate the index + static assets.
        with open(os.path.join(self._dir, "index.html"), "w") as f:
            f.write(self._index_page(manifest))
        with open(os.path.join(self._dir, "style.css"), "w") as f:
            f.write(_CSS)
        open(os.path.join(self._dir, ".nojekyll"), "w").close()
        return post_rel

    # --- templates --------------------------------------------------------

    def _page(self, inner: str, css_href: str = "style.css") -> str:
        t = html.escape(self._title)
        return (
            "<!doctype html><html lang=en><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{t}</title><link rel=stylesheet href='{css_href}'></head><body>"
            f"<div class=wrap><header><a href='{self._home_href(css_href)}'>📈 {t}</a> "
            "<span class=tag>· autonomous AI crypto trading journal</span></header>"
            f"{inner}"
            f"<footer>{_DISCLAIMER}</footer></div></body></html>"
        )

    @staticmethod
    def _home_href(css_href: str) -> str:
        # Post pages live in posts/, so link home up one level.
        return "../index.html" if css_href.startswith("..") else "index.html"

    def _index_page(self, manifest: list[dict]) -> str:
        cards = []
        for m in manifest:
            cards.append(
                f"<a class=post-card href='{html.escape(m['file'])}'>"
                f"<div class=muted>{html.escape(m['date'])} · today {_pct_span(m.get('day_pct', 0.0))}"
                f" · all-time {_pct_span(m.get('total_pct', 0.0))}</div>"
                f"<h2>{html.escape(m['title'])}</h2>"
                f"<p>{html.escape(m.get('summary', ''))}</p></a>"
            )
        body = (
            "<p class=muted>Daily findings &amp; learnings from a self-improving "
            "crypto trading bot — trades, reasoning, and what it got wrong.</p>"
            + ("".join(cards) or "<p class=muted>No posts yet.</p>")
        )
        return self._page(body, css_href="style.css")

    def _post_page(self, post: BlogPost) -> str:
        inner = (
            f"<article class=article><h1>{html.escape(post.title)}</h1>"
            f"<p class=muted>{html.escape(post.date)} · today {_pct_span(post.day_pct)}"
            f" · all-time {_pct_span(post.total_pct)}</p>"
            f"{post.body_html}</article>"
            "<p style='margin-top:20px'><a href='../index.html'>← all posts</a></p>"
        )
        return self._page(inner, css_href="../style.css")
