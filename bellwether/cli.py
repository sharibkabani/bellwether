"""Command-line interface.

    bellwether run      # start the always-on trading loop (+ daily reflection)
    bellwether once     # run a single cycle and print what happened
    bellwether reflect  # run the learning loop now: score, adapt, discover
    bellwether report   # build and (optionally) send today's digest
    bellwether status   # show current portfolio
    bellwether markets  # list the universe with quotes and signals
    bellwether news     # headlines the AI is reading, per coin, and what it concluded

All commands take ``--config path.yaml``. ``mode: kraken`` trades against real
Kraken prices; without ``--live`` it paper-fills (no keys, no risk), and with
``--live`` it places real orders (requires KRAKEN_API_KEY / KRAKEN_API_SECRET).
``mode: sim`` is a fully offline simulator.
"""

from __future__ import annotations

import argparse
import sys

from rich.console import Console
from rich.table import Table

from .config import load_config
from .factory import build_engine, build_news_inspector, build_notifier, build_reflector, build_trader
from .report import build_report, render_text

console = Console()


def _snapshot(venue, cfg):
    instruments = venue.list_instruments(cfg.strategy.categories or None)
    quotes = venue.quotes(instruments)
    return instruments, quotes


def cmd_once(args) -> int:
    cfg = load_config(args.config)
    trader, venue, portfolio, storage = build_trader(cfg, allow_live=args.live)
    _banner(cfg, args.live)
    report = trader.run_cycle()
    console.print(
        f"[bold]Cycle complete[/] — equity ${report.equity:,.2f}, "
        f"{len(report.entries)} entries, {len(report.exits)} exits, "
        f"{report.open_positions} open positions."
    )
    for f in report.entries + report.exits:
        console.print(
            f"  [cyan]{f.action.value.upper()}[/] {f.quantity} {f.symbol} "
            f"@ ${f.price:,.2f} — {f.rationale}"
        )
    if report.entries_skipped:
        console.print("[bold yellow]Could not reconcile the live wallet — new entries withheld this cycle.[/]")
    if report.halted:
        console.print("[bold red]Kill switch active — no new entries.[/]")
    storage.close()
    return 0


def cmd_run(args) -> int:
    cfg = load_config(args.config)
    trader, venue, portfolio, storage = build_trader(cfg, allow_live=args.live)
    notifier = build_notifier(cfg)
    reflector = build_reflector(cfg, storage, venue) if cfg.learning.enabled else None
    _banner(cfg, args.live)
    console.print(
        f"[green]Running[/] every {cfg.poll_interval_sec}s. "
        f"Daily report at {cfg.daily_report_hour}:00 via {cfg.notify.channel}. "
        + ("Learning loop ON. " if reflector else "")
        + "Ctrl-C to stop."
    )

    def on_cycle(report):
        console.print(
            f"[dim]{_now()}[/] equity ${report.equity:,.2f} · "
            f"{len(report.entries)} in / {len(report.exits)} out · "
            f"{report.open_positions} open"
            + (" · [yellow]entries withheld (reconcile failed)[/]" if report.entries_skipped else "")
            + (" · [red]HALTED[/]" if report.halted else "")
        )

    def on_daily_report():
        # Reflect first (score predictions, update trust, tune within bounds,
        # discover coins) so today's changes show up in the digest.
        if reflector is not None:
            try:
                summary = reflector.run()
                console.print(
                    f"[magenta]Reflection:[/] scored {summary.scored}, "
                    f"{len(summary.changes)} self-adjustments"
                    + (f", +{len(summary.discovery.added)} on probation" if summary.discovery and summary.discovery.added else "")
                    + "."
                )
            except Exception as exc:
                console.print(f"[red]Reflection failed:[/] {exc}")
        _instruments, quotes = _snapshot(venue, cfg)
        data = build_report(portfolio, storage, quotes, cfg.risk.starting_bankroll, cfg.mode,
                         learning_enabled=cfg.learning.enabled,
                         discovery_enabled=cfg.learning.enabled and cfg.learning.discovery_enabled)
        try:
            notifier.send(data)
            console.print(f"[green]Daily report sent via {cfg.notify.channel}.[/]")
        except Exception as exc:
            console.print(f"[red]Report send failed:[/] {exc}")
        # Publish the public daily blog (fail-soft; never breaks the loop).
        if cfg.blog.enabled:
            from .blog import publish_blog

            msg = publish_blog(cfg, data)
            if msg:
                console.print(f"[blue]{msg}[/]")

    try:
        trader.run_forever(on_cycle=on_cycle, on_daily_report=on_daily_report)
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped.[/]")
    finally:
        storage.close()
    return 0


def cmd_report(args) -> int:
    cfg = load_config(args.config)
    trader, venue, portfolio, storage = build_trader(cfg, allow_live=args.live)
    _instruments, quotes = _snapshot(venue, cfg)
    data = build_report(portfolio, storage, quotes, cfg.risk.starting_bankroll, cfg.mode,
                         learning_enabled=cfg.learning.enabled,
                         discovery_enabled=cfg.learning.enabled and cfg.learning.discovery_enabled)
    if args.send:
        build_notifier(cfg).send(data)
        console.print(f"[green]Report sent via {cfg.notify.channel}.[/]")
    else:
        console.print(render_text(data))
    storage.close()
    return 0


def cmd_reflect(args) -> int:
    cfg = load_config(args.config)
    trader, venue, portfolio, storage = build_trader(cfg, allow_live=args.live)
    if not cfg.learning.enabled:
        console.print("[yellow]Learning is disabled (learning.enabled: false).[/]")
        storage.close()
        return 0
    reflector = build_reflector(cfg, storage, venue)
    summary = reflector.run()
    console.print(
        f"[bold]Reflection — {summary.day}[/]\n"
        f"  Scored this run: {summary.scored}\n"
        f"  Track record: {summary.overall_hit_rate:.0%} hit rate over "
        f"{summary.overall_samples} scored predictions"
    )
    for source, (n, hr, calib) in summary.by_source.items():
        tag = "overconfident" if calib > 0.05 else ("underconfident" if calib < -0.05 else "calibrated")
        console.print(f"    {source}: {hr:.0%} ({n} preds) — {tag}")
    if summary.changes:
        console.print("  [cyan]Self-adjustments (within hard bounds):[/]")
        for c in summary.changes:
            console.print(f"    {c.field}: {c.old_value:.2f} → {c.new_value:.2f} — {c.reason}")
    else:
        console.print("  Self-adjustments: none.")
    if summary.discovery and summary.discovery.changed:
        ds = summary.discovery
        if ds.added:
            console.print(f"  [green]New on probation:[/] {', '.join(ds.added)}")
        if ds.graduated:
            console.print(f"  [green]Graduated to active:[/] {', '.join(ds.graduated)}")
        if ds.retired:
            console.print(f"  [yellow]Retired:[/] {', '.join(ds.retired)}")
    if summary.lessons:
        console.print("  [bold]Journal lessons:[/]")
        for line in summary.lessons.splitlines():
            if line.strip():
                console.print(f"    {line.strip()}")
    storage.close()
    return 0


def cmd_blog(args) -> int:
    cfg = load_config(args.config)
    if not cfg.blog.enabled:
        console.print("[yellow]Blog is disabled (set blog.enabled: true in config).[/]")
        return 0
    trader, venue, portfolio, storage = build_trader(cfg, allow_live=args.live)
    _instruments, quotes = _snapshot(venue, cfg)
    data = build_report(portfolio, storage, quotes, cfg.risk.starting_bankroll, cfg.mode,
                         learning_enabled=cfg.learning.enabled,
                         discovery_enabled=cfg.learning.enabled and cfg.learning.discovery_enabled)
    from .blog import publish_blog

    msg = publish_blog(cfg, data, push=not args.preview)
    console.print(f"[blue]{msg}[/]" if msg else "[yellow]Blog disabled.[/]")
    storage.close()
    return 0


def cmd_status(args) -> int:
    cfg = load_config(args.config)
    trader, venue, portfolio, storage = build_trader(cfg, allow_live=args.live)
    _instruments, quotes = _snapshot(venue, cfg)
    data = build_report(portfolio, storage, quotes, cfg.risk.starting_bankroll, cfg.mode,
                         learning_enabled=cfg.learning.enabled,
                         discovery_enabled=cfg.learning.enabled and cfg.learning.discovery_enabled)
    console.print(render_text(data))
    storage.close()
    return 0


def cmd_markets(args) -> int:
    cfg = load_config(args.config)
    trader, venue, portfolio, storage = build_trader(cfg, allow_live=args.live)
    instruments, quotes = _snapshot(venue, cfg)
    ideas = {i.instrument.symbol: i for i in build_engine(cfg, storage).generate(instruments, quotes)}

    table = Table(title="Universe · quotes · signals")
    for col in ("Symbol", "Name", "Last", "Signal", "Exp.Return", "Conf"):
        table.add_column(col, overflow="fold")
    for inst in instruments:
        q = quotes.get(inst.symbol)
        idea = ideas.get(inst.symbol)
        last = f"${q.last:,.2f}" if q else "—"
        if idea:
            from .models import Direction

            signed = idea.expected_return * (1 if idea.direction is Direction.LONG else -1)
            table.add_row(
                inst.symbol, inst.name, last,
                idea.direction.value.upper(),
                f"{signed:+.1%}", f"{idea.confidence:.0%}",
            )
        else:
            table.add_row(inst.symbol, inst.name, last, "-", "-", "-")
    console.print(table)
    storage.close()
    return 0


def _age(dt_or_ts) -> str:
    import datetime as _d

    if dt_or_ts is None:
        return "?"
    if isinstance(dt_or_ts, (int, float)):
        dt_or_ts = _d.datetime.fromtimestamp(dt_or_ts, _d.timezone.utc)
    secs = (_d.datetime.now(_d.timezone.utc) - dt_or_ts).total_seconds()
    if secs < 3600:
        return f"{max(0, int(secs // 60))}m ago"
    if secs < 86400:
        return f"{secs / 3600:.0f}h ago"
    return f"{secs / 86400:.0f}d ago"


def cmd_news(args) -> int:
    """What news the AI signal is reading right now, which headlines get
    attached to which coin, and what the model concluded on its last run."""
    from .news import DEFAULT_FEEDS

    cfg = load_config(args.config)
    trader, venue, portfolio, storage = build_trader(cfg, allow_live=False)
    instruments, quotes = _snapshot(venue, cfg)
    inspector = build_news_inspector(cfg, storage)
    batch = instruments[: cfg.strategy.max_symbols_per_cycle]

    if not cfg.news.enabled:
        console.print("[yellow]News is disabled (news.enabled: false): the AI sees prices only.[/]")
    headlines = inspector.fetch_headlines()
    prompt, coin_news = inspector.build_prompt(batch, quotes, headlines)
    if args.prompt:
        console.print(prompt, markup=False, highlight=False)
        storage.close()
        return 0

    feeds = cfg.news.feeds or DEFAULT_FEEDS
    sources = sorted({h.source for h in headlines})
    freshest = _age(headlines[0].published) if headlines else "-"
    console.print(
        f"[bold]News feed[/] · {len(headlines)} headlines from "
        f"{len(sources)}/{len(feeds)} feeds ({', '.join(sources) or 'none reachable'}) · newest {freshest}"
    )

    # 1. Coin-specific news attached to each coin in the prompt.
    t = Table(title="Coin news the AI sees (attached under each coin)", show_lines=False)
    for col in ("Coin", "Headline", "Source", "Age"):
        t.add_column(col, overflow="fold")
    used_titles = set()
    for inst in batch:
        rel = coin_news.get(inst.symbol, [])
        if not rel:
            t.add_row(inst.symbol, "[dim]no coin-specific news: judged on price + general headlines[/]", "", "")
            continue
        for i, h in enumerate(rel):
            used_titles.add(h.title)
            t.add_row(inst.symbol if i == 0 else "", h.title, h.source, _age(h.published))
    console.print(t)

    # 2. General headlines sent with every request.
    general = headlines[: cfg.news.general]
    if general:
        g = Table(title="General headlines (sent for market context)")
        for col in ("Headline", "Source", "Age"):
            g.add_column(col, overflow="fold")
        for h in general:
            used_titles.add(h.title)
            g.add_row(h.title, h.source, _age(h.published))
        console.print(g)

    unused = [h for h in headlines if h.title not in used_titles]
    console.print(f"[dim]{len(unused)} other fetched headlines matched no coin and weren't sent"
                  + (" (shown below)" if args.all and unused else " (bw news --all to list)") + "[/]")
    if args.all:
        for h in unused:
            console.print(f"  [dim]· {h.title} ({h.source}, {_age(h.published)})[/]")

    # 3. What the model concluded last time (from the prediction journal).
    rows = storage._conn.execute(
        """SELECT p.symbol, p.expected_return, p.confidence, p.rationale, p.ts
           FROM predictions p
           JOIN (SELECT symbol, MAX(ts) AS ts FROM predictions WHERE source = 'trending' GROUP BY symbol) last
             ON p.symbol = last.symbol AND p.ts = last.ts
           WHERE p.source = 'trending'"""
    ).fetchall()
    if rows:
        by_sym = {r["symbol"]: r for r in rows}
        a = Table(title="What the AI concluded (latest call per coin)")
        for col in ("Coin", "Exp.", "Conf", "Why", "When"):
            a.add_column(col, overflow="fold")
        for inst in batch:
            r = by_sym.get(inst.symbol)
            if r is None:
                a.add_row(inst.symbol, "-", "-", "[dim]no AI call recorded[/]", "")
                continue
            a.add_row(inst.symbol, f"{r['expected_return']:+.1%}", f"{r['confidence']:.0%}",
                      r["rationale"] or "", _age(r["ts"]))
        console.print(a)
        newest = max(r["ts"] for r in rows)
        import time as _t
        if _t.time() - newest > 2 * cfg.strategy.trending_refresh_sec + cfg.poll_interval_sec:
            console.print("[yellow]The last AI call is older than expected: the AI signal may be failing "
                          "(check `journalctl -u bellwether | grep trending`).[/]")
    elif cfg.learning.enabled:
        console.print("[dim]No AI calls recorded yet.[/]")
    else:
        console.print("[dim]AI conclusions are only recorded when learning is enabled.[/]")
    storage.close()
    return 0


def _banner(cfg, live: bool = False) -> None:
    if not cfg.is_live:
        console.print("[bold green]● SIM MODE — offline simulator, no real money[/]")
    elif live:
        console.print("[bold red]● KRAKEN LIVE — placing real orders with real money[/]")
    else:
        console.print(
            "[bold yellow]● KRAKEN PAPER — real Kraken prices, simulated fills (no risk)[/]"
        )


def _now() -> str:
    import datetime

    return datetime.datetime.now().strftime("%H:%M:%S")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="bellwether", description=__doc__)
    parser.add_argument("--config", default="config.yaml", help="path to config YAML")
    parser.add_argument(
        "--live", action="store_true", help="place real orders on Kraken (real money; needs API keys)"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name, fn in [
        ("run", cmd_run),
        ("once", cmd_once),
        ("status", cmd_status),
        ("markets", cmd_markets),
        ("reflect", cmd_reflect),
    ]:
        p = sub.add_parser(name)
        p.set_defaults(func=fn)
    rep = sub.add_parser("report")
    rep.add_argument("--send", action="store_true", help="send via the notifier")
    rep.set_defaults(func=cmd_report)
    nws = sub.add_parser("news", help="headlines the AI is reading and what it concluded")
    nws.add_argument("--all", action="store_true", help="also list fetched headlines that weren't sent")
    nws.add_argument("--prompt", action="store_true", help="print the exact prompt the AI receives")
    nws.set_defaults(func=cmd_news)
    blg = sub.add_parser("blog")
    blg.add_argument("--preview", action="store_true", help="build the site locally without pushing")
    blg.set_defaults(func=cmd_blog)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except RuntimeError as exc:  # e.g. Kraken keys missing for live, or API error
        console.print(f"[bold red]{exc}[/]")
        return 2


if __name__ == "__main__":
    sys.exit(main())
