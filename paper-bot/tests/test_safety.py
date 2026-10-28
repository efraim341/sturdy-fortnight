"""Guards for the project's one hard rule: orders never leave the simulator.

These fail the build if anyone adds a broker/exchange library, an HTTP client,
or a setting that looks like broker credentials.
"""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"

FORBIDDEN_MODULES = {
    # HTTP / sockets: the app has no reason to call out anywhere
    "requests", "httpx", "aiohttp", "urllib", "urllib3", "http.client", "socket", "websocket",
    "websockets", "ftplib", "smtplib",
    # brokers and exchanges
    "alpaca", "alpaca_trade_api", "ib_insync", "ibapi", "ccxt", "binance", "oandapyV20",
    "tradier", "robin_stocks", "schwab", "td", "kiteconnect", "MetaTrader5",
}

FORBIDDEN_REQUIREMENTS = re.compile(
    r"^(alpaca|ib[-_]?insync|ibapi|ccxt|python-binance|binance|oanda|tradier|robin[-_]?stocks|"
    r"schwab|kiteconnect|metatrader|requests|aiohttp)\b",
    re.I | re.M,
)


def _imports(path: Path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.module


def test_app_imports_no_network_or_broker_modules():
    for py in APP.rglob("*.py"):
        for mod in _imports(py):
            top = mod.split(".")[0]
            assert mod not in FORBIDDEN_MODULES and top not in FORBIDDEN_MODULES, f"{py.name} imports {mod}"


def test_requirements_have_no_broker_or_http_client():
    req = (ROOT / "requirements.txt").read_text()
    assert not FORBIDDEN_REQUIREMENTS.search(req), FORBIDDEN_REQUIREMENTS.search(req).group(0)


def test_no_broker_credentials_in_settings():
    src = (APP / "config.py").read_text().upper()
    for word in ("API_KEY", "API_SECRET", "BROKER", "ALPACA", "BINANCE", "ACCOUNT_ID", "PASSWORD"):
        assert f'"{word}' not in src and f"{word}=" not in src


def test_every_response_says_simulated(client, auth):
    from conftest import hook
    assert hook(client, action="buy", symbol="X", price=1, qty=1, id="s").json()["simulated"] is True
    assert client.get("/api/state", headers=auth).json()["simulated"] is True
