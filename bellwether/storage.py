"""SQLite persistence for portfolio state and trade history.

Everything that must survive a restart lives here: cash, open positions, the
full fill log, daily-spend tracking for the risk limits, price history for the
momentum signal, and equity snapshots for the daily report.

It also holds the self-learning substrate: the prediction journal and its
scores, learned reliability weights per (strategy, coin), the daily reflections
(the bot's trading journal), the discovered-coin universe with probation status,
and the bot's bounded config overrides plus a changelog of every self-change.
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

            -- The prediction journal: every signal the engine produced, logged
            -- with the price at the time so it can be scored against reality
            -- once its horizon elapses. This is the substrate the bot learns from.
            CREATE TABLE IF NOT EXISTS predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL,
                source TEXT,            -- strategy name (momentum | trending | ...)
                symbol TEXT,
                expected_return REAL,   -- signed fraction the strategy predicted
                confidence REAL,        -- [0, 1]
                rationale TEXT,
                price REAL,             -- price at the moment of prediction
                horizon_sec REAL,       -- seconds until this should be scored
                scored INTEGER DEFAULT 0,
                actual_return REAL,     -- realized signed return at the horizon
                correct INTEGER,        -- 1 = predicted direction was right
                scored_ts REAL
            );
            CREATE INDEX IF NOT EXISTS idx_pred_scored ON predictions(scored, ts);
            CREATE INDEX IF NOT EXISTS idx_pred_source ON predictions(source, symbol, scored);

            -- Learned reliability: a bounded trust multiplier per (strategy, coin),
            -- derived from that source's historical hit rate and calibration.
            CREATE TABLE IF NOT EXISTS reliability (
                source TEXT,
                symbol TEXT,
                samples INTEGER,
                hit_rate REAL,
                calibration_error REAL,
                multiplier REAL,
                updated_ts REAL,
                PRIMARY KEY (source, symbol)
            );

            -- The bot's trading journal: a daily reflection where the model
            -- reviews its own scorecard and writes lessons for next time.
            CREATE TABLE IF NOT EXISTS reflections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL,
                day TEXT,
                lessons TEXT,
                scorecard TEXT          -- JSON snapshot of the stats behind the lessons
            );

            -- Coins the bot discovered on its own. New coins enter on probation
            -- (watched / tiny-size only) and graduate to active once they prove
            -- out; chronically illiquid or unprofitable coins are retired.
            CREATE TABLE IF NOT EXISTS discovered_universe (
                symbol TEXT PRIMARY KEY,
                pair TEXT,
                name TEXT,
                category TEXT,
                status TEXT,            -- probation | active | retired
                added_ts REAL,
                updated_ts REAL,
                notes TEXT
            );

            -- Bot-owned, bounded config overrides (e.g. min_confidence, strategy
            -- weights). Capital-protection limits are NEVER written here.
            CREATE TABLE IF NOT EXISTS config_overrides (
                key TEXT PRIMARY KEY,
                value REAL,
                updated_ts REAL
            );

            -- An audit trail of every self-change the bot made, surfaced in the
            -- daily email so a human always sees what it adjusted and why.
            CREATE TABLE IF NOT EXISTS changelog (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL,
                day TEXT,
                field TEXT,
                old_value TEXT,
                new_value TEXT,
                reason TEXT
            );
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

    def price_at_or_after(self, symbol: str, ts: float) -> float | None:
        """First recorded price for ``symbol`` at or after ``ts`` (for scoring)."""
        row = self._conn.execute(
            "SELECT price FROM prices WHERE symbol=? AND ts >= ? ORDER BY ts ASC LIMIT 1",
            (symbol, ts),
        ).fetchone()
        return row["price"] if row else None

    # --- prediction journal ----------------------------------------------

    def record_prediction(
        self,
        source: str,
        symbol: str,
        expected_return: float,
        confidence: float,
        rationale: str,
        price: float,
        horizon_sec: float,
        ts: float | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO predictions(ts, source, symbol, expected_return, confidence, "
            "rationale, price, horizon_sec, scored) VALUES(?,?,?,?,?,?,?,?,0)",
            (
                ts or time.time(),
                source,
                symbol,
                expected_return,
                confidence,
                rationale,
                price,
                horizon_sec,
            ),
        )
        self._conn.commit()

    def due_predictions(self, now: float) -> list[dict]:
        """Unscored predictions whose horizon has elapsed (ready to score)."""
        rows = self._conn.execute(
            "SELECT * FROM predictions WHERE scored=0 AND (ts + horizon_sec) <= ? ORDER BY ts",
            (now,),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_prediction_scored(
        self, pred_id: int, actual_return: float | None, correct: int | None, ts: float | None = None
    ) -> None:
        self._conn.execute(
            "UPDATE predictions SET scored=1, actual_return=?, correct=?, scored_ts=? WHERE id=?",
            (actual_return, correct, ts or time.time(), pred_id),
        )
        self._conn.commit()

    def scored_predictions(self, since_ts: float = 0.0) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM predictions WHERE scored=1 AND correct IS NOT NULL AND ts >= ? ORDER BY ts",
            (since_ts,),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- reliability weights ---------------------------------------------

    def set_reliability(
        self,
        source: str,
        symbol: str,
        samples: int,
        hit_rate: float,
        calibration_error: float,
        multiplier: float,
        ts: float | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO reliability(source, symbol, samples, hit_rate, calibration_error, "
            "multiplier, updated_ts) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(source, symbol) DO UPDATE SET samples=excluded.samples, "
            "hit_rate=excluded.hit_rate, calibration_error=excluded.calibration_error, "
            "multiplier=excluded.multiplier, updated_ts=excluded.updated_ts",
            (source, symbol, samples, hit_rate, calibration_error, multiplier, ts or time.time()),
        )
        self._conn.commit()

    def reliability_multiplier(self, source: str, symbol: str) -> float:
        row = self._conn.execute(
            "SELECT multiplier FROM reliability WHERE source=? AND symbol=?",
            (source, symbol),
        ).fetchone()
        return float(row["multiplier"]) if row else 1.0

    def all_reliability(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM reliability ORDER BY source, symbol"
        ).fetchall()
        return [dict(r) for r in rows]

    # --- reflections (the trading journal) -------------------------------

    def record_reflection(self, day: str, lessons: str, scorecard: str, ts: float | None = None) -> None:
        self._conn.execute(
            "INSERT INTO reflections(ts, day, lessons, scorecard) VALUES(?,?,?,?)",
            (ts or time.time(), day, lessons, scorecard),
        )
        self._conn.commit()

    def latest_reflection(self) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM reflections ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    # --- discovered universe ---------------------------------------------

    def upsert_discovered(
        self,
        symbol: str,
        pair: str,
        name: str,
        category: str,
        status: str,
        notes: str = "",
        ts: float | None = None,
    ) -> None:
        now = ts or time.time()
        self._conn.execute(
            "INSERT INTO discovered_universe(symbol, pair, name, category, status, "
            "added_ts, updated_ts, notes) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(symbol) DO UPDATE SET pair=excluded.pair, name=excluded.name, "
            "category=excluded.category, status=excluded.status, "
            "updated_ts=excluded.updated_ts, notes=excluded.notes",
            (symbol, pair, name, category, status, now, now, notes),
        )
        self._conn.commit()

    def set_discovered_status(self, symbol: str, status: str, notes: str = "", ts: float | None = None) -> None:
        self._conn.execute(
            "UPDATE discovered_universe SET status=?, notes=?, updated_ts=? WHERE symbol=?",
            (status, notes, ts or time.time(), symbol),
        )
        self._conn.commit()

    def discovered(self, statuses: list[str] | None = None) -> list[dict]:
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            rows = self._conn.execute(
                f"SELECT * FROM discovered_universe WHERE status IN ({placeholders}) ORDER BY symbol",
                tuple(statuses),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM discovered_universe ORDER BY symbol"
            ).fetchall()
        return [dict(r) for r in rows]

    def probation_symbols(self) -> set[str]:
        rows = self._conn.execute(
            "SELECT symbol FROM discovered_universe WHERE status='probation'"
        ).fetchall()
        return {r["symbol"] for r in rows}

    # --- bot-owned config overrides + changelog --------------------------

    def get_override(self, key: str) -> float | None:
        row = self._conn.execute(
            "SELECT value FROM config_overrides WHERE key=?", (key,)
        ).fetchone()
        return float(row["value"]) if row else None

    def all_overrides(self) -> dict[str, float]:
        rows = self._conn.execute("SELECT key, value FROM config_overrides").fetchall()
        return {r["key"]: float(r["value"]) for r in rows}

    def set_override(self, key: str, value: float, ts: float | None = None) -> None:
        self._conn.execute(
            "INSERT INTO config_overrides(key, value, updated_ts) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ts=excluded.updated_ts",
            (key, value, ts or time.time()),
        )
        self._conn.commit()

    def record_change(
        self,
        day: str,
        field: str,
        old_value: str,
        new_value: str,
        reason: str,
        ts: float | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO changelog(ts, day, field, old_value, new_value, reason) VALUES(?,?,?,?,?,?)",
            (ts or time.time(), day, field, old_value, new_value, reason),
        )
        self._conn.commit()

    def changes_since(self, since_ts: float) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM changelog WHERE ts >= ? ORDER BY ts", (since_ts,)
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        self._conn.close()
