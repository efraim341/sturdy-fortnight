"""Settings, read from environment variables.

Every value has a safe default so the bot starts with no setup at all.
There is deliberately no setting for a broker, an exchange or an API key:
this project only ever trades inside its own simulator.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    return float(raw) if raw else default


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Secrets you create yourself (or let the cloud host generate).
    # Empty means "locked": the webhook / dashboard API refuse everything.
    webhook_secret: str
    dashboard_token: str

    starting_balance: float
    fee_rate: float          # 0.001 = 0.1% of the order value
    fee_fixed: float         # flat fee per order, in dollars
    default_order_usd: float # order size when a signal names no quantity
    max_order_usd: float     # any single order bigger than this is rejected
    max_daily_loss_usd: float  # new buys stop once today's realized loss reaches this
    default_stop_loss_pct: float  # stop placed this % below the buy price (0 = none)
    allow_fractional: bool   # fractional quantities (crypto) or whole shares only

    test_mode: bool          # dashboard can send fake signals
    dedup_window_seconds: int
    timezone: str            # what "today" means for the daily loss limit
    db_path: str


def load_settings() -> Settings:
    return Settings(
        webhook_secret=os.environ.get("WEBHOOK_SECRET", "").strip(),
        dashboard_token=os.environ.get("DASHBOARD_TOKEN", "").strip(),
        starting_balance=_float("STARTING_BALANCE", 10_000.0),
        fee_rate=_float("FEE_RATE", 0.001),
        fee_fixed=_float("FEE_FIXED", 0.0),
        default_order_usd=_float("DEFAULT_ORDER_USD", 1_000.0),
        max_order_usd=_float("MAX_ORDER_USD", 2_000.0),
        max_daily_loss_usd=_float("MAX_DAILY_LOSS_USD", 300.0),
        default_stop_loss_pct=_float("DEFAULT_STOP_LOSS_PCT", 2.0),
        allow_fractional=_bool("ALLOW_FRACTIONAL", True),
        test_mode=_bool("TEST_MODE", True),
        dedup_window_seconds=_int("DEDUP_WINDOW_SECONDS", 86_400),
        timezone=os.environ.get("TIMEZONE", "America/New_York").strip() or "America/New_York",
        db_path=os.environ.get("DB_PATH", "data/paperbot.db").strip() or "data/paperbot.db",
    )
