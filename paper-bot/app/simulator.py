"""The paper-trading simulator.

All "orders" are rows in the local SQLite database. Nothing here opens a
network connection or knows about any broker: a fill is just arithmetic on
the virtual cash balance at the price the signal carried.

Rules:
- Long only. "sell" closes (part of) a position you hold; it never goes short.
- Fees: fee_rate * order value + fee_fixed, on every buy and sell.
- Buy fees are folded into the position's average cost, so a sale's
  realized P&L is net of both fees.
- Stop loss: every signal that carries a price for a symbol first checks
  that symbol's stop. If the price is at or below the stop, the whole
  position is sold at that price (like a real stop, it can fill below the
  stop level when the price gaps).
- Limits: an order worth more than max_order_usd is rejected; once today's
  realized loss reaches max_daily_loss_usd, new buys are rejected for the
  rest of the day (sells and stops still work, so you can always get out).
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator

from .config import Settings
from .storage import Store

Action = Literal["buy", "sell", "close", "price"]
Source = Literal["webhook", "test"]

MONEY_EPS = 1e-9


class Signal(BaseModel):
    """One trading signal, from TradingView or from the test panel."""

    action: Action
    symbol: str = Field(min_length=1, max_length=40)
    price: float = Field(gt=0, lt=1e9)
    qty: Optional[float] = Field(default=None, gt=0, lt=1e9)
    usd: Optional[float] = Field(default=None, gt=0, lt=1e9)
    stop_loss: Optional[float] = Field(default=None, gt=0, lt=1e9)
    stop_loss_pct: Optional[float] = Field(default=None, ge=0, lt=100)
    id: Optional[str] = Field(default=None, max_length=200)

    @field_validator("symbol")
    @classmethod
    def _clean_symbol(cls, v: str) -> str:
        v = v.strip().upper()
        # "BINANCE:BTCUSDT" -> "BTCUSDT"
        if ":" in v:
            v = v.split(":", 1)[1]
        if not v or not all(c.isalnum() or c in "._-!/" for c in v):
            raise ValueError("symbol may only contain letters, digits and . _ - ! /")
        return v

    @field_validator("action", mode="before")
    @classmethod
    def _lower_action(cls, v: Any) -> Any:
        return v.strip().lower() if isinstance(v, str) else v

    def dedup_key(self) -> str:
        """The signal's own id, or a fingerprint of its content."""
        if self.id:
            return "id:" + self.id.strip()
        body = json.dumps(self.model_dump(exclude={"id"}), sort_keys=True)
        return "sha:" + hashlib.sha256(body.encode()).hexdigest()


@dataclass
class Result:
    status: Literal["filled", "rejected", "duplicate", "ok"]
    message: str
    orders: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "simulated": True,  # always: this project cannot place real orders
            "status": self.status,
            "message": self.message,
            "orders": self.orders,
        }


def _r(x: float, places: int = 8) -> float:
    return round(x + 0.0, places)


class Simulator:
    def __init__(self, settings: Settings, store: Store) -> None:
        self.s = settings
        self.store = store
        self.tz = ZoneInfo(settings.timezone)
        with self.store.tx() as db:
            self._ensure_account(db, time.time())

    # ---------- helpers ----------

    def _ensure_account(self, db, now: float) -> None:
        row = db.execute("SELECT id FROM account WHERE id = 1").fetchone()
        if row is None:
            db.execute(
                "INSERT INTO account (id, cash, starting_balance, created_at) VALUES (1, ?, ?, ?)",
                (self.s.starting_balance, self.s.starting_balance, now),
            )

    def _day(self, now: float) -> str:
        return datetime.fromtimestamp(now, self.tz).date().isoformat()

    def _fee(self, value: float) -> float:
        return _r(value * self.s.fee_rate + self.s.fee_fixed, 6)

    def _cash(self, db) -> float:
        return db.execute("SELECT cash FROM account WHERE id = 1").fetchone()["cash"]

    def _today_realized(self, db, now: float) -> float:
        row = db.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) AS p FROM orders "
            "WHERE status = 'filled' AND day = ?",
            (self._day(now),),
        ).fetchone()
        return row["p"]

    def _record(self, db, now: float, source: str, action: str, symbol: str, qty: float,
                price: float, fee: float, pnl: float, status: str, reason: str,
                signal_id: Optional[str]) -> dict:
        cur = db.execute(
            "INSERT INTO orders (ts, day, source, action, symbol, qty, price, fee, realized_pnl, "
            "status, reason, signal_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now, self._day(now), source, action, symbol, _r(qty), price, fee, _r(pnl, 6),
             status, reason, signal_id),
        )
        return {
            "id": cur.lastrowid, "ts": now, "source": source, "action": action,
            "symbol": symbol, "qty": _r(qty), "price": price, "fee": fee,
            "realized_pnl": _r(pnl, 6), "status": status, "reason": reason,
        }

    def _reject(self, db, now, source, action, sig: Signal, reason: str, qty: float = 0.0) -> Result:
        order = self._record(db, now, source, action, sig.symbol, qty, sig.price, 0.0, 0.0,
                             "rejected", reason, sig.id)
        return Result("rejected", reason, [order])

    def _sell(self, db, now, source, symbol, qty, price, reason, signal_id) -> dict:
        pos = db.execute("SELECT * FROM positions WHERE symbol = ?", (symbol,)).fetchone()
        value = qty * price
        fee = self._fee(value)
        pnl = value - fee - qty * pos["avg_price"]
        db.execute("UPDATE account SET cash = cash + ? WHERE id = 1", (_r(value - fee, 6),))
        left = pos["qty"] - qty
        if left <= MONEY_EPS:
            db.execute("DELETE FROM positions WHERE symbol = ?", (symbol,))
        else:
            db.execute("UPDATE positions SET qty = ?, last_price = ? WHERE symbol = ?",
                       (_r(left), price, symbol))
        return self._record(db, now, source, "sell", symbol, qty, price, fee, pnl,
                            "filled", reason, signal_id)

    def _check_stop(self, db, now, sig: Signal) -> list[dict]:
        pos = db.execute("SELECT * FROM positions WHERE symbol = ?", (sig.symbol,)).fetchone()
        if pos is None:
            return []
        db.execute("UPDATE positions SET last_price = ? WHERE symbol = ?", (sig.price, sig.symbol))
        stop = pos["stop_price"]
        if stop is None or sig.price > stop:
            return []
        reason = f"stop loss: price {sig.price:g} reached stop {stop:g}"
        return [self._sell(db, now, "stop_loss", sig.symbol, pos["qty"], sig.price, reason, sig.id)]

    def _buy_qty(self, sig: Signal) -> float:
        if sig.qty is not None:
            qty = sig.qty
        else:
            usd = sig.usd if sig.usd is not None else self.s.default_order_usd
            qty = usd / sig.price
        if self.s.allow_fractional:
            return math.floor(qty * 1e6) / 1e6
        return float(math.floor(qty + 1e-9))

    def _stop_for(self, sig: Signal) -> Optional[float]:
        if sig.stop_loss is not None:
            return sig.stop_loss
        pct = sig.stop_loss_pct if sig.stop_loss_pct is not None else self.s.default_stop_loss_pct
        if pct <= 0:
            return None
        return _r(sig.price * (1 - pct / 100), 6)

    # ---------- public API ----------

    def process(self, sig: Signal, source: Source, now: Optional[float] = None) -> Result:
        now = time.time() if now is None else now
        with self.store.tx() as db:
            # Duplicate check and execution happen in one transaction, so two
            # copies of the same alert arriving together can't both fill.
            db.execute("DELETE FROM seen_signals WHERE ts < ?", (now - self.s.dedup_window_seconds,))
            key = sig.dedup_key()
            if db.execute("SELECT 1 FROM seen_signals WHERE key = ?", (key,)).fetchone():
                return Result("duplicate", "duplicate signal ignored")
            db.execute("INSERT INTO seen_signals (key, ts) VALUES (?, ?)", (key, now))

            fills = self._check_stop(db, now, sig)

            if sig.action == "price":
                msg = "price updated" + (" - stop loss triggered" if fills else "")
                return Result("filled" if fills else "ok", msg, fills)

            if sig.action == "buy":
                res = self._buy(db, now, source, sig)
            else:
                res = self._sell_signal(db, now, source, sig)
            res.orders = fills + res.orders
            return res

    def _buy(self, db, now, source, sig: Signal) -> Result:
        qty = self._buy_qty(sig)
        if qty <= 0:
            return self._reject(db, now, source, "buy", sig, "quantity rounds to zero")
        value = qty * sig.price
        fee = self._fee(value)
        if value > self.s.max_order_usd + MONEY_EPS:
            return self._reject(db, now, source, "buy", sig,
                                f"order ${value:,.2f} is over the ${self.s.max_order_usd:,.2f} limit", qty)
        today = self._today_realized(db, now)
        if today <= -self.s.max_daily_loss_usd + MONEY_EPS and self.s.max_daily_loss_usd > 0:
            return self._reject(db, now, source, "buy", sig,
                                f"daily loss limit reached (today ${today:,.2f}); no new buys until tomorrow",
                                qty)
        if self._cash(db) + MONEY_EPS < value + fee:
            return self._reject(db, now, source, "buy", sig,
                                f"not enough virtual cash for ${value + fee:,.2f}", qty)
        stop = self._stop_for(sig)
        if stop is not None and stop >= sig.price:
            return self._reject(db, now, source, "buy", sig, "stop loss must be below the buy price", qty)

        db.execute("UPDATE account SET cash = cash - ? WHERE id = 1", (_r(value + fee, 6),))
        pos = db.execute("SELECT * FROM positions WHERE symbol = ?", (sig.symbol,)).fetchone()
        if pos is None:
            db.execute(
                "INSERT INTO positions (symbol, qty, avg_price, stop_price, last_price, opened_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (sig.symbol, qty, _r((value + fee) / qty), stop, sig.price, now),
            )
        else:
            new_qty = pos["qty"] + qty
            avg = (pos["qty"] * pos["avg_price"] + value + fee) / new_qty
            db.execute(
                "UPDATE positions SET qty = ?, avg_price = ?, stop_price = ?, last_price = ? WHERE symbol = ?",
                (_r(new_qty), _r(avg), stop if stop is not None else pos["stop_price"],
                 sig.price, sig.symbol),
            )
        reason = "bought" + (f", stop loss at {stop:g}" if stop is not None else ", no stop loss")
        order = self._record(db, now, source, "buy", sig.symbol, qty, sig.price, fee, 0.0,
                             "filled", reason, sig.id)
        return Result("filled", reason, [order])

    def _sell_signal(self, db, now, source, sig: Signal) -> Result:
        pos = db.execute("SELECT * FROM positions WHERE symbol = ?", (sig.symbol,)).fetchone()
        if pos is None:
            return self._reject(db, now, source, "sell", sig,
                                "no open position to sell (short selling is not supported)", sig.qty or 0.0)
        qty = pos["qty"] if sig.action == "close" or sig.qty is None else min(sig.qty, pos["qty"])
        if qty * sig.price > self.s.max_order_usd + MONEY_EPS and qty < pos["qty"]:
            # Closing a whole position is always allowed; partial sells obey the size limit.
            return self._reject(db, now, source, "sell", sig,
                                f"order is over the ${self.s.max_order_usd:,.2f} limit", qty)
        order = self._sell(db, now, source, sig.symbol, qty, sig.price, "sold", sig.id)
        return Result("filled", "sold", [order])

    def set_stop(self, symbol: str, stop: Optional[float]) -> bool:
        with self.store.tx() as db:
            cur = db.execute("UPDATE positions SET stop_price = ? WHERE symbol = ?",
                             (stop, symbol.strip().upper()))
            return cur.rowcount > 0

    def reset(self) -> None:
        with self.store.tx() as db:
            for table in ("account", "positions", "orders", "seen_signals"):
                db.execute(f"DELETE FROM {table}")
            self._ensure_account(db, time.time())

    def state(self, order_limit: int = 200, now: Optional[float] = None) -> dict:
        now = time.time() if now is None else now
        with self.store.tx() as db:
            acct = db.execute("SELECT * FROM account WHERE id = 1").fetchone()
            positions = []
            market_value = 0.0
            for p in db.execute("SELECT * FROM positions ORDER BY opened_at"):
                mv = p["qty"] * p["last_price"]
                market_value += mv
                positions.append({
                    "symbol": p["symbol"], "qty": p["qty"], "avg_price": p["avg_price"],
                    "last_price": p["last_price"], "stop_price": p["stop_price"],
                    "market_value": _r(mv, 2),
                    "unrealized_pnl": _r(mv - p["qty"] * p["avg_price"], 2),
                    "opened_at": p["opened_at"],
                })
            agg = db.execute(
                "SELECT COALESCE(SUM(fee), 0) AS fees, COALESCE(SUM(realized_pnl), 0) AS pnl, "
                "COUNT(*) AS n FROM orders WHERE status = 'filled'"
            ).fetchone()
            rejected = db.execute("SELECT COUNT(*) AS n FROM orders WHERE status = 'rejected'").fetchone()["n"]
            today = self._today_realized(db, now)
            orders = [dict(r) for r in db.execute(
                "SELECT id, ts, source, action, symbol, qty, price, fee, realized_pnl, status, reason "
                "FROM orders ORDER BY id DESC LIMIT ?", (order_limit,))]
            equity = acct["cash"] + market_value
            return {
                "simulated": True,
                "cash": _r(acct["cash"], 2),
                "equity": _r(equity, 2),
                "starting_balance": acct["starting_balance"],
                "total_pnl": _r(equity - acct["starting_balance"], 2),
                "realized_pnl": _r(agg["pnl"], 2),
                "today_realized_pnl": _r(today, 2),
                "fees_paid": _r(agg["fees"], 2),
                "filled_orders": agg["n"],
                "rejected_orders": rejected,
                "buys_blocked_today": self.s.max_daily_loss_usd > 0
                and today <= -self.s.max_daily_loss_usd + MONEY_EPS,
                "positions": positions,
                "orders": orders,
                "limits": {
                    "max_order_usd": self.s.max_order_usd,
                    "max_daily_loss_usd": self.s.max_daily_loss_usd,
                    "default_order_usd": self.s.default_order_usd,
                    "default_stop_loss_pct": self.s.default_stop_loss_pct,
                    "fee_rate": self.s.fee_rate,
                    "fee_fixed": self.s.fee_fixed,
                },
                "test_mode": self.s.test_mode,
                "trading_day": self._day(now),
                "timezone": self.s.timezone,
            }
