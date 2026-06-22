"""Portfolio state: cash, signed positions, and realized P&L.

Handles general signed-position accounting so long and short are uniform:
buying adds to a long (or covers a short), selling reduces a long (or opens a
short). Reducing or closing realizes P&L against the average cost; a fill that
flips the position books the realized P&L on the closed portion and re-bases the
remainder at the fill price. Every change is mirrored to SQLite.
"""

from __future__ import annotations

from .models import Action, Fill, Position, Quote
from .storage import Storage


class Portfolio:
    def __init__(self, storage: Storage, starting_cash: float):
        self._storage = storage
        self._positions = storage.load_positions()

        cash = storage.get_meta("cash")
        if cash is None:
            self._cash = starting_cash
            storage.set_meta("cash", str(starting_cash))
            storage.set_meta("realized_pnl", "0.0")
            storage.set_meta("peak_equity", str(starting_cash))
            storage.set_meta("baseline_equity", str(starting_cash))
        else:
            self._cash = float(cash)
        self._realized = float(storage.get_meta("realized_pnl", "0.0"))

    # --- read-only views --------------------------------------------------

    @property
    def cash(self) -> float:
        return self._cash

    @property
    def realized_pnl(self) -> float:
        return self._realized

    def positions(self) -> list[Position]:
        return list(self._positions.values())

    def position(self, symbol: str) -> Position | None:
        return self._positions.get(symbol)

    def exposure(self) -> float:
        """Gross dollar exposure across all open positions (long + |short|)."""
        return sum(abs(p.cost_basis()) for p in self._positions.values())

    def equity(self, quotes: dict[str, Quote]) -> float:
        """Cash plus the signed market value of every open position."""
        value = self._cash
        for pos in self._positions.values():
            q = quotes.get(pos.symbol)
            value += pos.market_value(q) if q is not None else pos.cost_basis()
        return value

    # --- mutation ---------------------------------------------------------

    def apply_fill(self, fill: Fill) -> float:
        """Apply a fill. Returns realized P&L from this fill (USD)."""
        self._storage.record_fill(fill)

        delta = fill.quantity if fill.action is Action.BUY else -fill.quantity
        notional = fill.quantity * fill.price
        if fill.action is Action.BUY:
            self._cash -= notional + fill.commission
        else:
            self._cash += notional - fill.commission

        pos = self._positions.get(fill.symbol)
        old_qty = pos.quantity if pos else 0
        old_avg = pos.avg_cost if pos else 0.0
        new_qty = old_qty + delta
        realized = 0.0

        reducing = old_qty != 0 and (old_qty > 0) != (delta > 0)
        if reducing:
            closed = min(abs(delta), abs(old_qty))
            # Long: profit when sell price > cost. Short: profit when buy price < cost.
            realized = closed * (fill.price - old_avg) * (1 if old_qty > 0 else -1)
            self._realized += realized

        if abs(new_qty) < 1e-9:  # closed (float dust tolerance)
            self._positions.pop(fill.symbol, None)
            self._storage.delete_position(fill.symbol)
        else:
            if old_qty == 0 or (old_qty > 0) == (delta > 0):
                # Opening, or adding in the same direction → weighted-average cost.
                total = abs(old_qty) * old_avg + fill.quantity * fill.price
                new_avg = total / abs(new_qty)
            elif abs(delta) <= abs(old_qty):
                # Reduced but not closed → average cost unchanged.
                new_avg = old_avg
            else:
                # Flipped direction → remainder opens fresh at the fill price.
                new_avg = fill.price
            new_pos = Position(fill.symbol, new_qty, new_avg)
            self._positions[fill.symbol] = new_pos
            self._storage.save_position(new_pos)

        self._storage.set_meta("cash", str(self._cash))
        self._storage.set_meta("realized_pnl", str(self._realized))
        return realized

    def update_peak_equity(self, equity: float) -> float:
        peak = float(self._storage.get_meta("peak_equity", str(equity)))
        if equity > peak:
            peak = equity
            self._storage.set_meta("peak_equity", str(peak))
        return peak

    # --- live reconciliation ---------------------------------------------

    _DUST_USD = 1.0  # ignore balances worth less than this

    def reconcile(self, cash: float, balances: dict[str, float], quotes: dict[str, Quote]) -> None:
        """Overwrite local state with the broker's truth (live trading).

        ``cash`` and ``balances`` (symbol -> real coin quantity) come from the
        venue. Quantities are trusted as-is; the average cost of a position the
        bot already tracks is kept (for stop-loss/take-profit), while a holding
        the bot didn't open is adopted at the current price (neutral basis, so
        it doesn't immediately trip an exit). Positions the broker no longer
        shows are dropped. Self-correcting: any drift from fees, partial fills,
        or manual deposits/withdrawals is resolved every cycle.
        """
        self._cash = cash
        self._storage.set_meta("cash", str(cash))

        # Determine the real, non-dust positions.
        real: dict[str, tuple[float, float]] = {}
        for symbol, qty in balances.items():
            if qty <= 0:
                continue
            q = quotes.get(symbol)
            price = q.last if q else 0.0
            if price and qty * price < self._DUST_USD:
                continue  # dust
            real[symbol] = (qty, price)

        for symbol, (qty, price) in real.items():
            existing = self._positions.get(symbol)
            if existing is not None:
                existing.quantity = qty
                if existing.avg_cost <= 0 and price:
                    existing.avg_cost = price
                self._storage.save_position(existing)
            else:
                pos = Position(symbol, qty, price)  # adopt at current price
                self._positions[symbol] = pos
                self._storage.save_position(pos)

        # Drop anything the broker no longer reports.
        for symbol in list(self._positions):
            if symbol not in real:
                del self._positions[symbol]
                self._storage.delete_position(symbol)

        # On the first successful live sync, re-baseline P&L to the real wallet
        # so the daily report measures gains since the bot took over (not since
        # the config's notional starting_bankroll).
        if self._storage.get_meta("live_baseline_set") != "1":
            self._storage.set_meta("baseline_equity", str(self.equity(quotes)))
            self._storage.set_meta("live_baseline_set", "1")
