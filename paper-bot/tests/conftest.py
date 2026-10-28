import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Importing app.main builds a default app; keep it off the real data folder.
os.environ.setdefault("DB_PATH", ":memory:")

from fastapi.testclient import TestClient  # noqa: E402

from app.config import load_settings  # noqa: E402
from app.main import create_app  # noqa: E402

SECRET = "test-webhook-secret"
TOKEN = "test-dashboard-token"


@pytest.fixture
def settings():
    return replace(
        load_settings(),
        webhook_secret=SECRET,
        dashboard_token=TOKEN,
        starting_balance=10_000.0,
        fee_rate=0.001,
        fee_fixed=0.0,
        default_order_usd=1_000.0,
        max_order_usd=2_000.0,
        max_daily_loss_usd=300.0,
        default_stop_loss_pct=2.0,
        allow_fractional=True,
        test_mode=True,
        dedup_window_seconds=86_400,
        timezone="America/New_York",
        db_path=":memory:",
    )


@pytest.fixture
def client(settings):
    return TestClient(create_app(settings))


@pytest.fixture
def auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def hook(client, **signal):
    body = {"secret": SECRET, **signal}
    return client.post("/webhook", json=body)
