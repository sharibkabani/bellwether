"""Risk manager: turn trade ideas into sized, capped orders — and exit losers.

Every order passes through here. It enforces hard limits (per trade, gross
exposure, daily spend, open-position count), sizes positions by conviction (a
base fraction of equity scaled by confidence), closes positions that hit
stop-loss or take-profit, and trips a kill switch on excessive drawdown.
Shorting is off by default — short ideas are skipped unless explicitly enabled.
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

    # --- exits ------------------------------------------------------------

    def check_exits(self, portfolio: Portfolio, quotes: dict[str, Quote]) -> list[Order]:
        orders = []
        for pos in portfolio.positions():
            quote = quotes.get(pos.symbol)
            if quote is None:
                continue
            pnl_pct = pos.unrealized_pnl_pct(quote)
            reason = None
            if pnl_pct <= -self._cfg.stop_loss_pct:
                reason = f"stop-loss {pnl_pct:.1%}"
            elif pnl_pct >= self._cfg.take_profit_pct:
                reason = f"take-profit {pnl_pct:+.1%}"
            if not reason:
                continue

            # Close: sell a long, buy back a short.
            action = Action.SELL if pos.is_long else Action.BUY
            price = quote.fill_price(action)
            limit = round(price * (1 + _SLIPPAGE if action is Action.BUY else 1 - _SLIPPAGE), 8)
            orders.append(
                Order(
                    symbol=pos.symbol,
                    action=action,
                    quantity=abs(pos.quantity),
                    order_type=OrderType.LIMIT,
                    limit_price=limit,
                    rationale=reason,
                )
            )
        return orders
