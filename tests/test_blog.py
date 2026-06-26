import os
import subprocess
import tempfile
from types import SimpleNamespace

from bellwether.blog.generator import BlogGenerator, md_to_html
from bellwether.blog.publisher import publish_blog
from bellwether.blog.site import SiteBuilder
from bellwether.config import Config
from bellwether.models import Action, Fill, Position, Quote


def _report(day=1.5, total=-2.0):
    return SimpleNamespace(
        date="2026-06-23",
        day_pnl_pct=day,
        total_pnl_pct=total,
        equity=4993.16,
        positions=[(Position("SOL", 6.7, 72.67), Quote("SOL", last=74.0, bid=73.9, ask=74.1))],
        trades_today=[
            Fill("SOL", Action.BUY, 6.7, 72.67, rationale="validator partnerships trending"),
        ],
        lessons="I was overconfident on memecoins; my BTC news calls were well-calibrated.",
        config_changes=[],
        discovered=[],
    )


# --- markdown -> html ---

def test_md_to_html_basic():
    h = md_to_html("# Title\n\nHello **world** and *yes*.\n\n- one\n- two")
    assert "<h1>Title</h1>" in h
    assert "<strong>world</strong>" in h and "<em>yes</em>" in h
    assert "<ul><li>one</li><li>two</li></ul>" in h


def test_md_to_html_escapes_injected_html():
    h = md_to_html("Look <script>alert(1)</script> here")
    assert "<script>" not in h            # the real tag must be escaped
    assert "&lt;script&gt;" in h


# --- generator (no LLM -> templated fallback) ---

def test_fallback_post_has_no_dollars_by_default():
    post = BlogGenerator(client=None, include_dollars=False).write(_report(day=1.5))
    assert post.date == "2026-06-23"
    assert "$" not in post.body_html and "$" not in post.summary
    assert "1.5" in post.body_html or "1.50" in post.body_html  # the % shows
    assert "SOL" in post.body_html


def test_generator_uses_llm_when_available():
    class Stub:
        name = "stub"
        def complete_json(self, *a, **k): return "{}"
        def complete_text(self, system, user):
            return "# SOL leads the day\n\nGood day, learned to trust BTC news more."
    post = BlogGenerator(client=Stub()).write(_report())
    assert post.title == "SOL leads the day"
    assert "BTC news" in post.body_html


# --- site builder ---

def test_site_builder_writes_files_and_dedupes():
    d = tempfile.mkdtemp()
    sb = SiteBuilder(d, "Bellwether Journal")
    post = BlogGenerator(client=None).write(_report())
    sb.add_post(post)
    for f in ("index.html", "style.css", ".nojekyll", f"posts/{post.date}.html"):
        assert os.path.exists(os.path.join(d, f)), f
    assert post.title in open(os.path.join(d, "index.html")).read()
    # Re-adding the same date overwrites, doesn't duplicate.
    sb.add_post(post)
    import json
    manifest = json.load(open(os.path.join(d, "posts.json")))
    assert len(manifest) == 1


# --- end-to-end publish to a local bare repo (no network) ---

def test_publish_pushes_to_git_remote(monkeypatch):
    bare = tempfile.mkdtemp()
    subprocess.run(["git", "init", "--bare", bare], check=True, capture_output=True)

    cfg = Config(mode="sim", data_dir=tempfile.mkdtemp())
    cfg.llm.enabled = False                 # no network — use templated post
    cfg.blog.enabled = True
    cfg.blog.repo_url = f"file://{bare}"
    cfg.blog.branch = "main"
    monkeypatch.setenv("GITHUB_TOKEN", "dummy")  # enables the push path (ignored for file://)

    msg = publish_blog(cfg, _report())
    assert msg and "published" in msg

    # Verify the commit actually landed by cloning the bare repo.
    checkout = tempfile.mkdtemp()
    subprocess.run(["git", "clone", "-b", "main", f"file://{bare}", checkout], check=True, capture_output=True)
    assert os.path.exists(os.path.join(checkout, "index.html"))
    assert os.path.exists(os.path.join(checkout, "posts", "2026-06-23.html"))


def test_publish_to_orphan_gh_pages_excludes_code(monkeypatch):
    # A repo that already has code on main; the blog must go to an orphan
    # gh-pages branch containing only the site — never the code.
    bare = tempfile.mkdtemp()
    subprocess.run(["git", "init", "--bare", "-b", "main", bare], check=True, capture_output=True)
    seed = tempfile.mkdtemp()
    subprocess.run(["git", "clone", f"file://{bare}", seed], check=True, capture_output=True)
    with open(os.path.join(seed, "trader.py"), "w") as f:
        f.write("# secret source code\n")
    for args in (["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "code"],
                 ["push", "origin", "main"]):
        subprocess.run(["git", "-C", seed, *args], check=True, capture_output=True)

    cfg = Config(mode="sim", data_dir=tempfile.mkdtemp())
    cfg.llm.enabled = False
    cfg.blog.enabled = True
    cfg.blog.repo_url = f"file://{bare}"
    cfg.blog.branch = "gh-pages"
    monkeypatch.setenv("GITHUB_TOKEN", "dummy")

    assert "published" in (publish_blog(cfg, _report()) or "")

    checkout = tempfile.mkdtemp()
    subprocess.run(["git", "clone", "-b", "gh-pages", f"file://{bare}", checkout], check=True, capture_output=True)
    assert os.path.exists(os.path.join(checkout, "index.html"))   # site is there
    assert not os.path.exists(os.path.join(checkout, "trader.py"))  # code is NOT
    # main still has the code, untouched.
    main_co = tempfile.mkdtemp()
    subprocess.run(["git", "clone", "-b", "main", f"file://{bare}", main_co], check=True, capture_output=True)
    assert os.path.exists(os.path.join(main_co, "trader.py"))


def test_publish_disabled_returns_none():
    cfg = Config(data_dir=tempfile.mkdtemp())
    assert cfg.blog.enabled is False
    assert publish_blog(cfg, _report()) is None
