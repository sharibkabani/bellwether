"""Command-line interface.

    bellwether run      # start the always-on trading loop
    bellwether once     # run a single cycle and print what happened
    bellwether report   # build and (optionally) send today's digest
    bellwether status   # show current portfolio
    bellwether markets  # list the universe with quotes and signals

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
from .factory import build_engine, build_notifier, build_trader
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
    _banner(cfg, args.live)
    console.print(
        f"[green]Running[/] every {cfg.poll_interval_sec}s. "
        f"Daily report at {cfg.daily_report_hour}:00 via {cfg.notify.channel}. "
        "Ctrl-C to stop."
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
        _instruments, quotes = _snapshot(venue, cfg)
        data = build_report(portfolio, storage, quotes, cfg.risk.starting_bankroll, cfg.mode)
        try:
            notifier.send(data)
            console.print(f"[green]Daily report sent via {cfg.notify.channel}.[/]")
        except Exception as exc:
            console.print(f"[red]Report send failed:[/] {exc}")

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
    data = build_report(portfolio, storage, quotes, cfg.risk.starting_bankroll, cfg.mode)
    if args.send:
        build_notifier(cfg).send(data)
        console.print(f"[green]Report sent via {cfg.notify.channel}.[/]")
    else:
        console.print(render_text(data))
    storage.close()
    return 0


def cmd_status(args) -> int:
    cfg = load_config(args.config)
    trader, venue, portfolio, storage = build_trader(cfg, allow_live=args.live)
    _instruments, quotes = _snapshot(venue, cfg)
    data = build_report(portfolio, storage, quotes, cfg.risk.starting_bankroll, cfg.mode)
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
    ]:
        p = sub.add_parser(name)
        p.set_defaults(func=fn)
    rep = sub.add_parser("report")
    rep.add_argument("--send", action="store_true", help="send via the notifier")
    rep.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except RuntimeError as exc:  # e.g. Kraken keys missing for live, or API error
        console.print(f"[bold red]{exc}[/]")
        return 2


if __name__ == "__main__":
    sys.exit(main())
