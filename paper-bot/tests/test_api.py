from dataclasses import replace

from fastapi.testclient import TestClient

from app.main import create_app
from conftest import SECRET, hook


def test_health(client):
    assert client.get("/health").json() == {"ok": True, "simulated": True}


def test_dashboard_page_has_no_secrets(client, settings):
    html = client.get("/").text
    assert "בוט מסחר" in html
    assert settings.webhook_secret not in html and settings.dashboard_token not in html


def test_webhook_buy_then_state(client, auth):
    r = hook(client, action="buy", symbol="AAPL", price=200, usd=1000, id="t1")
    assert r.status_code == 200 and r.json()["status"] == "filled" and r.json()["simulated"]
    st = client.get("/api/state", headers=auth).json()
    assert st["positions"][0]["symbol"] == "AAPL" and st["positions"][0]["qty"] == 5


def test_webhook_wrong_or_missing_secret(client, auth):
    assert client.post("/webhook", json={"action": "buy", "symbol": "A", "price": 1}).status_code == 401
    assert client.post("/webhook", json={"secret": "nope", "action": "buy", "symbol": "A", "price": 1}).status_code == 401
    assert client.get("/api/state", headers=auth).json()["orders"] == []


def test_webhook_locked_without_secret(settings):
    c = TestClient(create_app(replace(settings, webhook_secret="")))
    r = c.post("/webhook", json={"secret": "", "action": "buy", "symbol": "A", "price": 1})
    assert r.status_code == 503


def test_webhook_accepts_text_plain(client):
    # TradingView sends text/plain bodies
    body = '{"secret": "%s", "action": "buy", "symbol": "AAPL", "price": 100, "qty": 1}' % SECRET
    r = client.post("/webhook", content=body, headers={"Content-Type": "text/plain"})
    assert r.status_code == 200 and r.json()["status"] == "filled"


def test_webhook_bad_json_and_bad_fields(client):
    assert client.post("/webhook", content="buy AAPL").status_code == 400
    assert hook(client, action="moon", symbol="AAPL", price=1).status_code == 422
    assert hook(client, action="buy", symbol="AAPL", price=-5).status_code == 422
    assert client.post("/webhook", content="x" * 20_000).status_code == 413


def test_webhook_duplicates(client):
    assert hook(client, action="buy", symbol="AAPL", price=100, qty=1, id="dup").json()["status"] == "filled"
    assert hook(client, action="buy", symbol="AAPL", price=100, qty=1, id="dup").json()["status"] == "duplicate"


def test_dashboard_api_needs_token(client):
    assert client.get("/api/state").status_code == 401
    assert client.get("/api/state", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_test_mode_signal(client, auth):
    r = client.post("/api/test/signal", headers=auth, json={"action": "buy", "symbol": "TSLA", "price": 250, "qty": 2})
    assert r.json()["status"] == "filled"
    # pressing the same button twice is two signals, not a duplicate
    r = client.post("/api/test/signal", headers=auth, json={"action": "buy", "symbol": "TSLA", "price": 250, "qty": 2})
    assert r.json()["status"] == "filled"
    st = client.get("/api/state", headers=auth).json()
    assert st["positions"][0]["qty"] == 4 and st["orders"][0]["source"] == "test"


def test_test_mode_off(settings, auth):
    c = TestClient(create_app(replace(settings, test_mode=False)))
    r = c.post("/api/test/signal", headers=auth, json={"action": "buy", "symbol": "TSLA", "price": 250})
    assert r.status_code == 403


def test_move_stop_and_reset(client, auth):
    hook(client, action="buy", symbol="AAPL", price=100, qty=1, id="a")
    assert client.post("/api/positions/stop", headers=auth, json={"symbol": "AAPL", "stop_price": 90}).status_code == 200
    assert client.get("/api/state", headers=auth).json()["positions"][0]["stop_price"] == 90
    assert client.post("/api/positions/stop", headers=auth, json={"symbol": "NOPE", "stop_price": 1}).status_code == 404
    assert client.post("/api/reset", headers=auth, json={"confirm": "yes"}).status_code == 400
    assert client.post("/api/reset", headers=auth, json={"confirm": "RESET"}).status_code == 200
    assert client.get("/api/state", headers=auth).json()["cash"] == 10_000
