"""Live Kraken venue.

Kraken is registered with Canadian regulators (a Restricted Dealer under the
OSC/CSA), trades cheaply, and exposes a clean REST API — so a Canadian can run
a real, self-serve crypto trading bot through it. This client uses:

  • the **public** market-data endpoint (``/0/public/Ticker``) for quotes — no
    credentials needed, so it works even in paper mode against real prices;
  • the **private** endpoints (``/0/private/AddOrder``, ``/Balance``) for live
    orders, authenticated with Kraken's API-Key + HMAC-SHA512 signature scheme.

Two modes share this class:
  • ``paper=True`` (default) — fetches **real** Kraken prices but **simulates**
    fills locally. No keys, no money at risk. The safe default for ``mode:
    kraken`` runs without ``--live``.
  • ``paper=False`` — places **real orders** with real money. Requires
    KRAKEN_API_KEY / KRAKEN_API_SECRET and the explicit --live flag.

Kraken spot is long-only, which matches the bot's long-only default.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import urllib.parse

import requests

from ..models import Action, Fill, Instrument, Order, OrderType, Quote

_FEE_RATE = 0.0026  # Kraken taker fee, used to model paper-fill commission


class KrakenError(RuntimeError):
    pass


def _round_price(price: float) -> float:
    """Round a limit price to a precision Kraken will accept for the magnitude."""
    if price >= 1000:
        return round(price, 1)
    if price >= 10:
        return round(price, 2)
    if price >= 1:
        return round(price, 4)
    return round(price, 6)


class KrakenVenue:
    name = "kraken"

    def __init__(
        self,
        instruments: list[Instrument],
        api_key: str = "",
        api_secret: str = "",
        paper: bool = True,
        api_base: str = "https://api.kraken.com",
        starting_cash: float = 0.0,
    ):
        self._universe = instruments
        self._pairs = {i.symbol: (i.pair or f"{i.symbol}USD") for i in instruments}
        self._key = api_key
        self._secret = api_secret
        self._paper = paper
        self._base = api_base.rstrip("/")
        self._starting_cash = starting_cash
        self._session = requests.Session()
        self.reconciles = not paper  # live accounts reconcile against the real wallet
        self._base_asset_cache: dict[str, str] | None = None

        if not paper and (not api_key or not api_secret):
            raise KrakenError(
                "Live Kraken trading requires KRAKEN_API_KEY and "
                "KRAKEN_API_SECRET environment variables."
            )

    # --- HTTP / auth ------------------------------------------------------

    def _public(self, method: str, params: dict) -> dict:
        resp = self._session.get(f"{self._base}/0/public/{method}", params=params, timeout=15)
        data = resp.json()
        if data.get("error"):
            raise KrakenError(f"{method}: {data['error']}")
        return data["result"]

    def _sign(self, path: str, data: dict) -> str:
        postdata = urllib.parse.urlencode(data)
        encoded = (str(data["nonce"]) + postdata).encode()
        message = path.encode() + hashlib.sha256(encoded).digest()
        mac = hmac.new(base64.b64decode(self._secret), message, hashlib.sha512)
        return base64.b64encode(mac.digest()).decode()

    def _private(self, method: str, data: dict) -> dict:
        path = f"/0/private/{method}"
        data = {**data, "nonce": int(time.time() * 1000)}
        headers = {"API-Key": self._key, "API-Sign": self._sign(path, data)}
        resp = self._session.post(self._base + path, headers=headers, data=data, timeout=15)
        body = resp.json()
        if body.get("error"):
            raise KrakenError(f"{method}: {body['error']}")
        return body["result"]

    # --- quotes -----------------------------------------------------------

    def _ticker(self, pair: str) -> Quote | None:
        try:
            result = self._public("Ticker", {"pair": pair})
        except (requests.RequestException, KrakenError):
            return None
        if not result:
            return None
        t = next(iter(result.values()))  # single requested pair
        try:
            return Quote(
                symbol=pair,
                last=float(t["c"][0]),   # last trade price
                bid=float(t["b"][0]),
                ask=float(t["a"][0]),
                volume=int(float(t["v"][1])),  # 24h volume
            )
        except (KeyError, IndexError, ValueError):
            return None

    def list_instruments(self, categories: list[str] | None = None) -> list[Instrument]:
        return [
            i for i in self._universe
            if not categories or i.category in categories
        ]

    def quotes(self, instruments: list[Instrument]) -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        for inst in instruments:
            pair = self._pairs.get(inst.symbol, f"{inst.symbol}USD")
            q = self._ticker(pair)
            if q is not None:
                q.symbol = inst.symbol  # report under the friendly ticker
                out[inst.symbol] = q
        return out

    # --- orders -----------------------------------------------------------

    def place_order(self, order: Order) -> Fill | None:
        pair = order.pair or self._pairs.get(order.symbol, f"{order.symbol}USD")
        quote = self._ticker(pair)
        if quote is None:
            return None
        fill_price = quote.fill_price(order.action)

        # Respect the limit price.
        if order.limit_price is not None:
            if order.action is Action.BUY and fill_price > order.limit_price:
                return None
            if order.action is Action.SELL and fill_price < order.limit_price:
                return None

        if self._paper:
            # Real prices, simulated fill. Cash is enforced upstream by the
            # risk manager against the local portfolio.
            return Fill(
                symbol=order.symbol,
                action=order.action,
                quantity=order.quantity,
                price=fill_price,
                commission=order.quantity * fill_price * _FEE_RATE,
                rationale=order.rationale,
            )

        # Live order.
        body = {
            "pair": pair,
            "type": order.action.value,            # buy / sell
            "ordertype": order.order_type.value.lower(),  # limit / market
            "volume": f"{order.quantity:.8f}",
        }
        if order.order_type is OrderType.LIMIT and order.limit_price is not None:
            body["price"] = str(_round_price(order.limit_price))
        try:
            self._private("AddOrder", body)
        except KrakenError:
            return None
        # AddOrder doesn't return a synchronous fill price; book at the limit
        # (or last) price. Reconciliation with Kraken fills is left as a follow-up.
        price = order.limit_price or fill_price
        return Fill(
            symbol=order.symbol,
            action=order.action,
            quantity=order.quantity,
            price=price,
            commission=order.quantity * price * _FEE_RATE,
            rationale=order.rationale,
        )

    def balance(self) -> float:
        if self._paper:
            return self._starting_cash
        try:
            result = self._private("Balance", {})
        except KrakenError:
            return 0.0
        return float(result.get("ZUSD", result.get("USD", 0.0)))

    # --- account reconciliation (live only) ------------------------------

    def _base_assets(self) -> dict[str, str]:
        """Map each universe symbol -> Kraken's base-asset code (e.g. BTC ->
        XXBT), resolved from the public AssetPairs endpoint and cached. Using
        Kraken's own pair->base mapping avoids guessing the X-prefix quirks."""
        if self._base_asset_cache is not None:
            return self._base_asset_cache
        pairs = ",".join(self._pairs.values())
        try:
            result = self._public("AssetPairs", {"pair": pairs})
        except Exception:
            return {}  # any error → no map → caller skips reconciliation (safe)
        altname_to_base = {v.get("altname"): v.get("base") for v in result.values()}
        cache: dict[str, str] = {}
        for symbol, pair in self._pairs.items():
            base = altname_to_base.get(pair)
            if base:
                cache[symbol] = base
        self._base_asset_cache = cache
        return cache

    def account_snapshot(self):
        """Return (real USD cash, {symbol: real coin quantity}) for live
        accounts, or None for paper / on error (caller skips reconciliation)."""
        if self._paper:
            return None
        base_assets = self._base_assets()
        if not base_assets:
            return None  # couldn't resolve asset codes; don't reconcile on bad data
        try:
            balances = self._private("Balance", {})
        except Exception:
            return None  # any error → skip reconciliation this cycle (safe)
        cash = float(balances.get("ZUSD", balances.get("USD", 0.0)))
        positions: dict[str, float] = {}
        for symbol, base in base_assets.items():
            qty = float(balances.get(base, 0.0) or 0.0)
            if qty > 0:
                positions[symbol] = qty
        return cash, positions
