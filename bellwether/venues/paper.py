"""Offline paper-trading venue: a deterministic crypto-market simulator.

Used by ``mode: sim`` — fully offline, no network, no keys, no money. It prices
a universe of major coins with a bounded geometric random walk (each coin has a
small persistent drift, so some genuinely trend — the signal the strategies are
meant to catch) and fills orders at the bid/ask with Kraken-style percentage
fees. This lets the whole bot run and be tested anywhere; the live Kraken client
is a drop-in replacement.
"""

from __future__ import annotations

import hashlib

from ..models import Action, Fill, Instrument, Order, Quote

# Universe of major, liquid coins (USD prices).
_UNIVERSE = [
    ("BTC", "Bitcoin", "Major", 62000.0),
    ("ETH", "Ethereum", "Major", 3050.0),
    ("SOL", "Solana", "L1", 168.0),
    ("XRP", "XRP", "Payments", 0.62),
    ("DOGE", "Dogecoin", "Meme", 0.16),
    ("ADA", "Cardano", "L1", 0.58),
    ("AVAX", "Avalanche", "L1", 38.0),
    ("LINK", "Chainlink", "DeFi", 18.5),
    ("MATIC", "Polygon", "L2", 0.72),
    ("DOT", "Polkadot", "L1", 7.1),
]

_FEE_RATE = 0.0026  # Kraken-style 0.26% taker fee


class PaperVenue:
    name = "paper"
    reconciles = False  # no real account; the local portfolio is authoritative

    def account_snapshot(self):
        return None

    def __init__(self, starting_cash: float, seed: int = 11):
        self._cash = starting_cash
        self._seed = seed
        self._tick = 0
        self._prices: dict[str, float] = {s: p for s, _, _, p in _UNIVERSE}
        self._prev_close: dict[str, float] = dict(self._prices)

    def _drift(self, symbol: str) -> float:
        h = hashlib.sha256(f"drift:{symbol}:{self._seed}".encode()).digest()
        return ((h[0] / 255.0) - 0.5) * 0.020  # ~±1.0%/tick (crypto is volatile)

    def _advance(self) -> None:
        self._tick += 1
        for symbol, price in self._prices.items():
            h = hashlib.sha256(f"{symbol}:{self._seed}:{self._tick}".encode()).digest()
            noise = ((h[0] / 255.0) - 0.5) * 0.030  # ±1.5% noise
            self._prices[symbol] = max(1e-6, price * (1 + self._drift(symbol) + noise))

    def _quote(self, symbol: str) -> Quote:
        last = self._prices[symbol]
        spread = max(1e-6, last * 0.0005)
        h = hashlib.sha256(f"vol:{symbol}:{self._tick}".encode()).digest()
        return Quote(
            symbol=symbol,
            last=round(last, 6),
            bid=round(last - spread, 6),
            ask=round(last + spread, 6),
            volume=int(50_000 + h[0] * 1000),
            prev_close=round(self._prev_close[symbol], 6),
        )

    # --- Venue interface --------------------------------------------------

    def list_instruments(self, categories: list[str] | None = None) -> list[Instrument]:
        self._advance()
        out = []
        for symbol, name, category, _ in _UNIVERSE:
            if categories and category not in categories:
                continue
            out.append(Instrument(symbol=symbol, name=name, category=category))
        return out

    def quotes(self, instruments: list[Instrument]) -> dict[str, Quote]:
        return {i.symbol: self._quote(i.symbol) for i in instruments if i.symbol in self._prices}

    def discover_candidates(self, min_volume_usd: float = 0.0) -> list[dict]:
        """Expose the full simulated market as discovery candidates. Lets the
        learning loop "find" coins (e.g. MATIC, DOT) that aren't in the default
        config universe — so discovery is exercisable fully offline."""
        out = []
        for symbol, name, category, _ in _UNIVERSE:
            q = self._quote(symbol)
            volume_usd = q.volume * q.last
            if volume_usd < min_volume_usd:
                continue
            change = (q.last / q.prev_close - 1.0) if q.prev_close else 0.0
            out.append(
                {
                    "symbol": symbol,
                    "pair": f"{symbol}USD",
                    "name": name,
                    "volume_usd": volume_usd,
                    "change_24h": change,
                    "last": q.last,
                }
            )
        return out

    def place_order(self, order: Order) -> Fill | None:
        if order.symbol not in self._prices:
            return None
        quote = self._quote(order.symbol)
        price = quote.fill_price(order.action)
        if order.limit_price is not None:
            if order.action is Action.BUY and price > order.limit_price:
                return None
            if order.action is Action.SELL and price < order.limit_price:
                return None

        commission = order.quantity * price * _FEE_RATE
        if order.action is Action.BUY:
            if order.quantity * price + commission > self._cash:
                return None
            self._cash -= order.quantity * price + commission
        else:
            self._cash += order.quantity * price - commission

        return Fill(
            symbol=order.symbol,
            action=order.action,
            quantity=order.quantity,
            price=price,
            commission=commission,
            rationale=order.rationale,
        )

    def balance(self) -> float:
        return self._cash
