"""Turn a day's ReportData into a readable blog post.

The model writes the post in the bot's first-person voice from a *sanitized*
context — percentages, symbols, directions, rationale, and the lessons it
journaled, but (by default) no dollar amounts or position sizes, so the public
page never leaks the account's real size. If no LLM is available or the call
fails, a clean templated post is built from the same structured data, so the
blog never blocks on the model.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

from ..models import Action
from ..signals.llm import LLMClient


@dataclass
class BlogPost:
    date: str           # YYYY-MM-DD
    title: str
    summary: str        # plain-text teaser for the index
    body_html: str
    day_pct: float
    total_pct: float


_SYSTEM = (
    "You are Bellwether, an autonomous crypto trading bot keeping a public daily "
    "journal. Write a short (200-350 word) first-person post about today: how you "
    "did, the notable moves you made and the reasoning, and — most importantly — "
    "what you LEARNED and how you're adjusting. Be honest about losses; candor is "
    "the point. Engaging but factual, no hype. CRITICAL: never mention specific "
    "dollar amounts, account balance, or position sizes — speak only in percentages "
    "and direction. Output Markdown: a single '# ' title line, then the body."
)


class BlogGenerator:
    def __init__(self, client: LLMClient | None = None, include_dollars: bool = False):
        self._client = client
        self._include_dollars = include_dollars

    def write(self, report) -> BlogPost:
        ctx = self._context(report)
        md = None
        if self._client is not None:
            try:
                md = self._client.complete_text(_SYSTEM, ctx)
            except Exception:
                md = None
        if not md or "#" not in md:
            md = self._fallback_markdown(report, ctx)

        title, body_md = _split_title(md, default=f"Trading journal — {report.date}")
        body_html = md_to_html(body_md)
        summary = _first_paragraph(body_md)
        return BlogPost(
            date=report.date,
            title=title,
            summary=summary,
            body_html=body_html,
            day_pct=report.day_pnl_pct,
            total_pct=report.total_pnl_pct,
        )

    # --- context (sanitized) ---------------------------------------------

    def _context(self, report) -> str:
        lines = [
            f"Date: {report.date}",
            f"Today's return: {report.day_pnl_pct:+.2f}%",
            f"All-time return: {report.total_pnl_pct:+.2f}%",
            f"Open positions: {len(report.positions)}",
        ]
        if self._include_dollars:
            lines.append(f"Equity: ${report.equity:,.2f}")

        trades = []
        for f in report.trades_today:
            verb = "opened a long position in" if f.action is Action.BUY else "sold / trimmed"
            r = (getattr(f, "rationale", "") or "").strip()
            trades.append(f"  - {verb} {f.symbol}" + (f" — {r}" if r else ""))
        if trades:
            lines.append("Trades today:")
            lines.extend(trades)
        else:
            lines.append("Trades today: none.")

        pos = []
        for p, q in report.positions:
            try:
                pct = p.unrealized_pnl_pct(q) if q else 0.0
                pos.append(f"  - {p.symbol} {p.direction.value}: {pct:+.1%} unrealized")
            except Exception:
                pos.append(f"  - {p.symbol}")
        if pos:
            lines.append("Open positions:")
            lines.extend(pos)

        if getattr(report, "lessons", ""):
            lines.append("Lessons I journaled (from scoring my own past calls):")
            lines.append(report.lessons.strip())

        changes = _format_changes(getattr(report, "config_changes", []))
        if changes:
            lines.append("Self-adjustments I made today (within hard safety bounds):")
            lines.extend(f"  - {c}" for c in changes)

        disc = _format_discoveries(getattr(report, "discovered", []))
        if disc:
            lines.append(f"Coins on my watch/probation list: {disc}")

        return "\n".join(lines)

    def _fallback_markdown(self, report, ctx) -> str:
        """Deterministic post when no model is available."""
        verb = "up" if report.day_pnl_pct >= 0 else "down"
        title = f"# {report.date}: {verb} {abs(report.day_pnl_pct):.2f}% today"
        parts = [title, ""]
        parts.append(
            f"Today I finished **{report.day_pnl_pct:+.2f}%** (all-time "
            f"**{report.total_pnl_pct:+.2f}%**), holding {len(report.positions)} positions."
        )
        moves = [
            f"- {('Opened a long in' if f.action is Action.BUY else 'Sold/trimmed')} "
            f"**{f.symbol}** — {(getattr(f, 'rationale', '') or '').strip()}"
            for f in report.trades_today
        ]
        if moves:
            parts += ["", "## Moves", "", *moves]
        if getattr(report, "lessons", ""):
            parts += ["", "## What I learned", "", report.lessons.strip()]
        return "\n".join(parts)


# --- markdown -> safe HTML (no external deps) ----------------------------

def _split_title(md: str, default: str) -> tuple[str, str]:
    lines = md.strip().splitlines()
    title = default
    body_start = 0
    for i, ln in enumerate(lines):
        if ln.strip():
            if ln.strip().startswith("#"):
                title = ln.strip().lstrip("#").strip()
                body_start = i + 1
            break
    return title, "\n".join(lines[body_start:]).strip()


def _first_paragraph(md: str) -> str:
    for block in re.split(r"\n\s*\n", md.strip()):
        b = block.strip()
        if b and not b.startswith("#"):
            text = re.sub(r"[*_`#>\-]", "", b).strip()
            return (text[:200] + "…") if len(text) > 200 else text
    return ""


def _inline(text: str) -> str:
    # text is already HTML-escaped; markers (* _ [ ]) survive escaping.
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<em>\1</em>", text)
    text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
    # links: only http(s) targets
    text = re.sub(
        r"\[(.+?)\]\((https?://[^\s)]+)\)", r'<a href="\2" rel="nofollow">\1</a>', text
    )
    return text


def md_to_html(md: str) -> str:
    """Convert a safe subset of Markdown to HTML. Escapes first, so any HTML in
    the model output is rendered inert — safe to publish."""
    queue = re.split(r"\n\s*\n", html.escape(md.strip()))
    out: list[str] = []
    while queue:
        block = queue.pop(0)
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        # A heading may sit on the first line of a block with content under it
        # (no blank line). Emit the heading, then re-process the remainder.
        if lines[0].lstrip().startswith("#"):
            level = len(lines[0]) - len(lines[0].lstrip("#"))
            out.append(f"<h{min(max(level, 1), 4)}>{_inline(lines[0].lstrip('#').strip())}</h{min(max(level, 1), 4)}>")
            if len(lines) > 1:
                queue.insert(0, "\n".join(lines[1:]))
            continue
        if all(re.match(r"\s*[-*]\s+", ln) for ln in lines):
            items = ""
            for ln in lines:
                content = re.sub(r"^\s*[-*]\s+", "", ln)
                items += f"<li>{_inline(content)}</li>"
            out.append(f"<ul>{items}</ul>")
        else:
            out.append(f"<p>{_inline(' '.join(l.strip() for l in lines))}</p>")
    return "\n".join(out)


def _format_changes(changes) -> list[str]:
    out = []
    for c in changes or []:
        try:
            if isinstance(c, dict):
                field = c.get("field"); old = c.get("old_value"); new = c.get("new_value"); reason = c.get("reason", "")
            else:
                field = getattr(c, "field", None); old = getattr(c, "old_value", None)
                new = getattr(c, "new_value", None); reason = getattr(c, "reason", "")
            if field is None:
                continue
            out.append(f"{field}: {old} → {new}" + (f" ({reason})" if reason else ""))
        except Exception:
            continue
    return out


def _format_discoveries(discovered) -> str:
    syms = []
    for d in discovered or []:
        try:
            syms.append(d["symbol"] if isinstance(d, dict) else getattr(d, "symbol", str(d)))
        except Exception:
            continue
    return ", ".join(s for s in syms if s)
