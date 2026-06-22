"""SQLite persistence for portfolio state and trade history.

Everything that must survive a restart lives here: cash, open positions, the
full fill log, daily-spend tracking for the risk limits, price history for the
momentum signal, and equity snapshots for the daily report.
"""

from __future__ import annotations

import os
import sqlite3
import time

from .models import Action, Fill, Position


class Storage:
    def __init__(self, data_dir: str):
        os.makedirs(data_dir, exist_ok=True)
        self._path = os.path.join(data_dir, "bellwether.db")
        self._conn = sqlite3.connect(self._path)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS positions (
                symbol TEXT PRIMARY KEY,
                quantity REAL,
                avg_cost REAL
            );
            CREATE TABLE IF NOT EXISTS fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL,
                symbol TEXT,
                action TEXT,
                quantity REAL,
                price REAL,
                commission REAL,
                rationale TEXT
            );
            CREATE TABLE IF NOT EXISTS equity (
                ts REAL PRIMARY KEY,
                value REAL
            );
            CREATE TABLE IF NOT EXISTS prices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL,
                symbol TEXT,
                price REAL
            );
            CREATE INDEX IF NOT EXISTS idx_prices_symbol ON prices(symbol, id);
            """
        )
        self._conn.commit()

    # --- key/value meta ---------------------------------------------------

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self._conn.commit()

    # --- positions --------------------------------------------------------

    def load_positions(self) -> dict[str, Position]:
        out: dict[str, Position] = {}
        for row in self._conn.execute("SELECT * FROM positions"):
            out[row["symbol"]] = Position(
                symbol=row["symbol"],
                quantity=row["quantity"],
                avg_cost=row["avg_cost"],
            )
        return out

    def save_position(self, pos: Position) -> None:
        self._conn.execute(
            "INSERT INTO positions(symbol, quantity, avg_cost) VALUES(?,?,?) "
            "ON CONFLICT(symbol) DO UPDATE SET "
            "quantity=excluded.quantity, avg_cost=excluded.avg_cost",
            (pos.symbol, pos.quantity, pos.avg_cost),
        )
        self._conn.commit()

    def delete_position(self, symbol: str) -> None:
        self._conn.execute("DELETE FROM positions WHERE symbol=?", (symbol,))
        self._conn.commit()

    # --- fills ------------------------------------------------------------

    def record_fill(self, fill: Fill) -> None:
        self._conn.execute(
            "INSERT INTO fills(ts, symbol, action, quantity, price, commission, rationale) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                fill.ts,
                fill.symbol,
                fill.action.value,
                fill.quantity,
                fill.price,
                fill.commission,
                fill.rationale,
            ),
        )
        self._conn.commit()

    def fills_since(self, since_ts: float) -> list[Fill]:
        rows = self._conn.execute(
            "SELECT * FROM fills WHERE ts >= ? ORDER BY ts", (since_ts,)
        ).fetchall()
        return [
            Fill(
                symbol=r["symbol"],
                action=Action(r["action"]),
                quantity=r["quantity"],
                price=r["price"],
                ts=r["ts"],
                commission=r["commission"] or 0.0,
                rationale=r["rationale"] or "",
            )
            for r in rows
        ]

    # --- equity -----------------------------------------------------------

    def record_equity(self, value: float, ts: float | None = None) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO equity(ts, value) VALUES(?, ?)",
            (ts or time.time(), value),
        )
        self._conn.commit()

    def equity_since(self, since_ts: float) -> list[tuple[float, float]]:
        rows = self._conn.execute(
            "SELECT ts, value FROM equity WHERE ts >= ? ORDER BY ts", (since_ts,)
        ).fetchall()
        return [(r["ts"], r["value"]) for r in rows]

    # --- price history (for momentum across restarts) --------------------

    def record_price(self, symbol: str, price: float, ts: float | None = None) -> None:
        self._conn.execute(
            "INSERT INTO prices(ts, symbol, price) VALUES(?,?,?)",
            (ts or time.time(), symbol, price),
        )
        self._conn.commit()

    def recent_prices(self, symbol: str, limit: int) -> list[float]:
        rows = self._conn.execute(
            "SELECT price FROM prices WHERE symbol=? ORDER BY id DESC LIMIT ?",
            (symbol, limit),
        ).fetchall()
        return [r["price"] for r in reversed(rows)]  # oldest-first

    def close(self) -> None:
        self._conn.close()
