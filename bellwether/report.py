"""Daily report: the digest the user actually sees.

Summarizes the day in plain language — equity, P&L, what the bot bought and sold
and why, open positions (long/short, marked to market), and the strongest
signals it's watching. Three renderings share one data object: a rich terminal
panel, an HTML email, and a short SMS. A non-technical reader should understand
at a glance what the bot did with their money today.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field

from .models import Position, Quote
from .portfolio import Portfolio
from .storage import Storage


@dataclass
class ReportData:
    date: str
    equity: float
    day_pnl: float
    day_pnl_pct: float
    total_pnl: float
    total_pnl_pct: float
    cash: float
    starting_bankroll: float
    positions: list[tuple[Position, Quote | None]] = field(default_factory=list)
    trades_today: list = field(default_factory=list)
    halted: bool = False
    mode: str = "sim"


def _start_of_today_ts() -> float:
    today = _dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return today.timestamp()


def build_report(
    portfolio: Portfolio,
    storage: Storage,
    quotes: dict[str, Quote],
    starting_bankroll: float,
    mode: str = "sim",
    halted: bool = False,
) -> ReportData:
    equity = portfolio.equity(quotes)
    day_start_ts = _start_of_today_ts()
    snaps = storage.equity_since(day_start_ts)
    day_open = snaps[0][1] if snaps else equity
    day_pnl = equity - day_open
    # All-time P&L is measured from a baseline: the config starting_bankroll in
    # sim, or the real wallet equity captured on the first live reconcile.
    baseline = float(storage.get_meta("baseline_equity", str(starting_bankroll)))
    total_pnl = equity - baseline
    starting_bankroll = baseline

    positions = [
        (pos, quotes.get(pos.symbol))
        for pos in sorted(portfolio.positions(), key=lambda p: p.symbol)
    ]

    return ReportData(
        date=_dt.date.today().isoformat(),
        equity=equity,
        day_pnl=day_pnl,
        day_pnl_pct=(day_pnl / day_open * 100) if day_open else 0.0,
        total_pnl=total_pnl,
        total_pnl_pct=(total_pnl / starting_bankroll * 100) if starting_bankroll else 0.0,
        cash=portfolio.cash,
        starting_bankroll=starting_bankroll,
        positions=positions,
        trades_today=storage.fills_since(day_start_ts),
        halted=halted,
        mode=mode,
    )


def _money(x: float) -> str:
    return f"${x:,.2f}"


def _qty(x: float) -> str:
    """Format a (possibly fractional) crypto quantity, trimming trailing zeros."""
    s = f"{x:,.6f}".rstrip("0").rstrip(".")
    return s or "0"


def _signed(x: float) -> str:
    return f"{'+' if x >= 0 else '-'}${abs(x):,.2f}"


def render_text(d: ReportData) -> str:
    lines = [
        f"Bellwether daily report — {d.date}  [{d.mode.upper()}]",
        "=" * 52,
        f"Equity:        {_money(d.equity)}",
        f"Today's P&L:   {_signed(d.day_pnl)} ({d.day_pnl_pct:+.1f}%)",
        f"All-time P&L:  {_signed(d.total_pnl)} ({d.total_pnl_pct:+.1f}%)",
        f"Cash:          {_money(d.cash)}",
    ]
    if d.halted:
        lines.append("⚠️  KILL SWITCH ACTIVE — trading halted (max drawdown hit).")

    lines.append("")
    if d.trades_today:
        lines.append(f"Trades today ({len(d.trades_today)}):")
        for f in d.trades_today:
            lines.append(
                f"  {f.action.value.upper():4} {_qty(f.quantity)} {f.symbol} "
                f"@ {_money(f.price)} — {f.rationale}"
            )
    else:
        lines.append("Trades today: none.")

    lines.append("")
    if d.positions:
        lines.append(f"Open positions ({len(d.positions)}):")
        for pos, quote in d.positions:
            tag = "LONG " if pos.is_long else "SHORT"
            if quote is not None:
                upnl = pos.unrealized_pnl(quote)
                lines.append(
                    f"  {tag} {_qty(abs(pos.quantity))} {pos.symbol} "
                    f"@ {_money(pos.avg_cost)} (now {_money(quote.last)}) "
                    f"→ {_signed(upnl)} ({pos.unrealized_pnl_pct(quote):+.1%})"
                )
            else:
                lines.append(
                    f"  {tag} {_qty(abs(pos.quantity))} {pos.symbol} @ {_money(pos.avg_cost)}"
                )
    else:
        lines.append("Open positions: none.")

    return "\n".join(lines)


def render_html(d: ReportData) -> str:
    color = "#16a34a" if d.day_pnl >= 0 else "#dc2626"
    rows = ""
    for pos, quote in d.positions:
        upnl = pos.unrealized_pnl(quote) if quote else 0.0
        pc = "#16a34a" if upnl >= 0 else "#dc2626"
        now = _money(quote.last) if quote else "—"
        tag = "LONG" if pos.is_long else "SHORT"
        rows += (
            f"<tr><td>{pos.symbol}</td><td>{tag}</td><td>{_qty(abs(pos.quantity))}</td>"
            f"<td>{_money(pos.avg_cost)}</td><td>{now}</td>"
            f"<td style='color:{pc}'>{_signed(upnl)}</td></tr>"
        )
    trades = ""
    for f in d.trades_today:
        trades += (
            f"<li><b>{f.action.value.upper()}</b> {_qty(f.quantity)} {f.symbol} "
            f"@ {_money(f.price)} — <i>{f.rationale}</i></li>"
        )
    halted = (
        "<p style='color:#dc2626;font-weight:bold'>⚠️ Kill switch active — trading halted.</p>"
        if d.halted
        else ""
    )
    return f"""\
<div style="font-family:-apple-system,Segoe UI,sans-serif;max-width:640px;margin:auto">
  <h2 style="margin-bottom:0">Bellwether — {d.date}
    <span style="font-size:13px;color:#888">[{d.mode.upper()}]</span></h2>
  {halted}
  <p style="font-size:28px;font-weight:700;margin:8px 0">{_money(d.equity)}
    <span style="font-size:16px;color:{color}">{_signed(d.day_pnl)} ({d.day_pnl_pct:+.1f}%) today</span>
  </p>
  <p style="color:#555">All-time {_signed(d.total_pnl)} ({d.total_pnl_pct:+.1f}%) · Cash {_money(d.cash)}</p>
  <h3>Trades today ({len(d.trades_today)})</h3>
  <ul>{trades or '<li>None</li>'}</ul>
  <h3>Open positions ({len(d.positions)})</h3>
  <table cellpadding="6" style="border-collapse:collapse;width:100%">
    <tr style="text-align:left;border-bottom:1px solid #ddd">
      <th>Symbol</th><th>Side</th><th>Shares</th><th>Avg</th><th>Now</th><th>P&L</th></tr>
    {rows or '<tr><td colspan=6>None</td></tr>'}
  </table>
</div>"""


def render_sms(d: ReportData) -> str:
    halt = " HALTED" if d.halted else ""
    return (
        f"Bellwether {d.date}{halt}: equity {_money(d.equity)}, "
        f"today {_signed(d.day_pnl)} ({d.day_pnl_pct:+.1f}%), "
        f"{len(d.trades_today)} trades, {len(d.positions)} open."
    )
