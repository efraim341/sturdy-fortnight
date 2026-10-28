"""SQLite storage for the virtual account, positions, orders and seen signals."""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    cash REAL NOT NULL,
    starting_balance REAL NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT PRIMARY KEY,
    qty REAL NOT NULL,
    avg_price REAL NOT NULL,      -- cost per unit, buy fees included
    stop_price REAL,              -- NULL = no stop loss
    last_price REAL NOT NULL,
    opened_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    day TEXT NOT NULL,            -- local trading day, for the daily loss limit
    source TEXT NOT NULL,         -- webhook | test | stop_loss
    action TEXT NOT NULL,         -- buy | sell
    symbol TEXT NOT NULL,
    qty REAL NOT NULL,
    price REAL NOT NULL,
    fee REAL NOT NULL,
    realized_pnl REAL NOT NULL,   -- net of all fees; 0 for buys
    status TEXT NOT NULL,         -- filled | rejected
    reason TEXT NOT NULL,
    signal_id TEXT
);
CREATE TABLE IF NOT EXISTS seen_signals (
    key TEXT PRIMARY KEY,
    ts REAL NOT NULL
);
"""


class Store:
    """One connection guarded by a lock: simple and safe for a single small server."""

    def __init__(self, path: str) -> None:
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._conn.executescript(SCHEMA)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Run a block as one all-or-nothing transaction."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")
