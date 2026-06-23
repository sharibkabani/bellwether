"""Risk manager: turn trade ideas into sized, capped orders — and manage exits.

Every order passes through here. It enforces hard limits (per trade, gross
exposure, daily spend, open-position count), sizes positions by conviction (a
base fraction of equity scaled by confidence), manages exits, and trips a kill
switch on excessive drawdown. Shorting is off by default.

Exits are tuned for faster turnover with room for runners (long positions):
  • hard stop-loss     — cut a loser at ``-stop_loss_pct`` from cost;
  • partial take-profit — at ``+partial_take_profit_pct`` sell a fraction (bank
                          quick money), then let the rest ride;
  • trailing stop       — once a position has been up ``trail_activate_pct``,
                          exit the remainder if it falls ``trailing_stop_pct``
                          from its peak — so intraday spikes get banked, not
                          round-tripped;
  • hard take-profit    — a high ceiling that fully exits a big winner.
Per-position peak and partial-taken state lives in the meta KV store.
"""

from __future__ import annotations

import datetime as _dt

from .config import RiskConfig
from .models import Action, Direction, Order, OrderType, Quote, TradeIdea
from .portfolio import Portfolio
from .storage import Storage

_SLIPPAGE = 0.002  # 20 bps limit allowance so aggressive orders fill


class RiskManager:
    def __init__(self, cfg: RiskConfig, storage: Storage, probation_size_pct: float = 1.0):
        self._cfg = cfg
        self._storage = storage
        # Coins the learning loop is still vetting trade at a fraction of normal
        # size until they graduate. 1.0 disables the effect.
        self._probation_size_pct = probation_size_pct

    # --- daily spend tracking --------------------------------------------

    def _spend_key(self) -> str:
        return f"spend:{_dt.date.today().isoformat()}"

    def spent_today(self) -> float:
        return float(self._storage.get_meta(self._spend_key(), "0.0"))

    def record_entry_spend(self, amount: float) -> None:
        self._storage.set_meta(self._spend_key(), str(self.spent_today() + amount))

    def remaining_daily_spend(self) -> float:
        return max(0.0, self._cfg.max_daily_spend - self.spent_today())

    # --- kill switch ------------------------------------------------------

    def kill_switch_triggered(self, equity: float, peak_equity: float) -> bool:
        if peak_equity <= 0:
            return False
        return (peak_equity - equity) / peak_equity >= self._cfg.max_drawdown_pct

    # --- entry approval ---------------------------------------------------

    def approve_entries(
        self, ideas: list[TradeIdea], portfolio: Portfolio, quotes: dict[str, Quote]
    ) -> list[Order]:
        equity = portfolio.equity(quotes)
        peak = portfolio.update_peak_equity(equity)
        if self.kill_switch_triggered(equity, peak):
            return []

        orders: list[Order] = []
        open_count = len(portfolio.positions())
        exposure = portfolio.exposure()
        daily_budget = self.remaining_daily_spend()
        cash = portfolio.cash
        probation = (
            self._storage.probation_symbols() if self._probation_size_pct < 1.0 else set()
        )

        for idea in ideas:
            if open_count >= self._cfg.max_open_positions:
                break
            if (
                idea.expected_return < self._cfg.min_expected_return
                or idea.confidence < self._cfg.min_confidence
            ):
                continue
            if idea.direction is Direction.SHORT and not self._cfg.allow_short:
                continue
            if portfolio.position(idea.instrument.symbol):
                continue  # one position per symbol
            price = idea.entry_price
            if price <= 0:
                continue

            # Conviction sizing: a base fraction of equity, scaled by confidence.
            notional = equity * self._cfg.position_pct * idea.confidence
            if idea.instrument.symbol in probation:
                notional *= self._probation_size_pct  # tiny size while vetting
            notional = min(
                notional,
                self._cfg.max_position_per_trade,
                self._cfg.max_total_exposure - exposure,
                daily_budget,
                cash,  # cap by cash even for shorts (conservative)
            )
            qty = round(notional / price, 8)
            if qty <= 0 or qty * price < 1.0:  # below a sane minimum order size
                continue
            cost = qty * price

            action = idea.direction.action
            limit = round(price * (1 + _SLIPPAGE if action is Action.BUY else 1 - _SLIPPAGE), 8)

            # Fresh position → reset any stale trailing/partial state for this symbol.
            self._clear_exit_state(idea.instrument.symbol)

            orders.append(
                Order(
                    symbol=idea.instrument.symbol,
                    action=action,
                    quantity=qty,
                    order_type=OrderType.LIMIT,
                    limit_price=limit,
                    pair=idea.instrument.pair,
                    rationale=(
                        f"{idea.direction.value} exp {idea.expected_return:+.1%}, "
                        f"conf {idea.confidence:.0%} — {idea.rationale}"
                    ),
                )
            )
            open_count += 1
            exposure += cost
            daily_budget -= cost
            cash -= cost

        return orders

    # --- per-position exit state (peak + partial-taken) ------------------

    def _peak_key(self, symbol: str) -> str:
        return f"exit_peak:{symbol}"

    def _partial_key(self, symbol: str) -> str:
        return f"exit_partial:{symbol}"

    def _clear_exit_state(self, symbol: str) -> None:
        self._storage.delete_meta(self._peak_key(symbol))
        self._storage.delete_meta(self._partial_key(symbol))

    def _close_order(self, pos, quote: Quote, quantity: float, reason: str) -> Order:
        action = Action.SELL if pos.is_long else Action.BUY
        price = quote.fill_price(action)
        limit = round(price * (1 + _SLIPPAGE if action is Action.BUY else 1 - _SLIPPAGE), 8)
        return Order(
            symbol=pos.symbol,
            action=action,
            quantity=quantity,
            order_type=OrderType.LIMIT,
            limit_price=limit,
            rationale=reason,
        )

    # --- exits ------------------------------------------------------------

    def check_exits(self, portfolio: Portfolio, quotes: dict[str, Quote]) -> list[Order]:
        orders = []
        for pos in portfolio.positions():
            quote = quotes.get(pos.symbol)
            if quote is None or pos.avg_cost <= 0:
                continue
            pnl_pct = pos.unrealized_pnl_pct(quote)

            # 1. Hard stop-loss (applies to longs and shorts).
            if pnl_pct <= -self._cfg.stop_loss_pct:
                self._clear_exit_state(pos.symbol)
                orders.append(self._close_order(pos, quote, abs(pos.quantity), f"stop-loss {pnl_pct:.1%}"))
                continue
            # 2. Hard take-profit ceiling.
            if pnl_pct >= self._cfg.take_profit_pct:
                self._clear_exit_state(pos.symbol)
                orders.append(self._close_order(pos, quote, abs(pos.quantity), f"take-profit {pnl_pct:+.1%}"))
                continue

            # Trailing + partial only for long positions (Kraken spot default).
            if not pos.is_long:
                continue

            price = quote.last
            # Track the running peak; persist every cycle so a pullback can't make
            # the default recompute to a lower value and "forget" the high.
            stored = self._storage.get_meta(self._peak_key(pos.symbol))
            peak = max(float(stored) if stored is not None else pos.avg_cost, price)
            self._storage.set_meta(self._peak_key(pos.symbol), str(peak))
            peak_gain = peak / pos.avg_cost - 1.0
            drawdown_from_peak = (peak - price) / peak if peak > 0 else 0.0

            # 3. Trailing stop: once we've been up enough, protect the gains.
            if peak_gain >= self._cfg.trail_activate_pct and drawdown_from_peak >= self._cfg.trailing_stop_pct:
                self._clear_exit_state(pos.symbol)
                orders.append(
                    self._close_order(
                        pos, quote, abs(pos.quantity),
                        f"trailing stop: {pnl_pct:+.1%} ({drawdown_from_peak:.1%} off peak)",
                    )
                )
                continue

            # 4. Partial take-profit (once): bank a chunk, let the rest ride.
            partial_taken = self._storage.get_meta(self._partial_key(pos.symbol), "0") == "1"
            if not partial_taken and pnl_pct >= self._cfg.partial_take_profit_pct:
                qty = round(abs(pos.quantity) * self._cfg.partial_take_fraction, 8)
                if qty > 0 and qty * price >= 1.0:
                    self._storage.set_meta(self._partial_key(pos.symbol), "1")
                    orders.append(
                        self._close_order(
                            pos, quote, qty,
                            f"partial take-profit {pnl_pct:+.1%} ({self._cfg.partial_take_fraction:.0%})",
                        )
                    )
        return orders
