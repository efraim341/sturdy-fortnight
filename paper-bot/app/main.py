"""FastAPI server: TradingView webhook in, paper trades in the local simulator out.

Endpoints
    GET  /                      the iPhone dashboard (a page; the data needs the token)
    GET  /health                for the cloud host's health check
    POST /webhook               TradingView alerts (the JSON must carry "secret")
    GET  /api/state             balance, P&L, positions, order history   [token]
    POST /api/test/signal       send a fake signal (only when TEST_MODE) [token]
    POST /api/positions/stop    move or remove a position's stop loss    [token]
    POST /api/reset             start over with the starting balance     [token]

This server makes no outbound network calls. There is no broker client in
the code or in requirements.txt, so an order cannot leave the simulator.
"""

from __future__ import annotations

import hmac
import json
import logging
import secrets
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, ValidationError

from .config import Settings, load_settings
from .simulator import Signal, Simulator
from .storage import Store

log = logging.getLogger("paperbot")
STATIC = Path(__file__).parent / "static"
MAX_BODY_BYTES = 10_000


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


class StopUpdate(BaseModel):
    symbol: str = Field(min_length=1, max_length=40)
    stop_price: Optional[float] = Field(default=None, gt=0)


class ResetRequest(BaseModel):
    confirm: str


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or load_settings()
    sim = Simulator(settings, Store(settings.db_path))
    app = FastAPI(title="Paper Trading Bot (simulation only)", version="1.0.0")
    app.state.sim = sim
    app.state.settings = settings

    log.warning("PAPER TRADING ONLY: orders are simulated locally and never sent to a broker.")
    if not settings.webhook_secret:
        log.warning("WEBHOOK_SECRET is not set: /webhook rejects every request.")
    if not settings.dashboard_token:
        log.warning("DASHBOARD_TOKEN is not set: the dashboard API is locked.")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        return response

    def require_token(authorization: str = Header(default="")) -> None:
        if not settings.dashboard_token:
            raise HTTPException(503, "dashboard locked: set DASHBOARD_TOKEN on the server")
        token = authorization.removeprefix("Bearer ").strip()
        if not token or not _same(token, settings.dashboard_token):
            raise HTTPException(401, "wrong or missing dashboard token")

    @app.get("/", include_in_schema=False)
    def dashboard():
        return FileResponse(STATIC / "dashboard.html", media_type="text/html")

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest():
        return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/health")
    def health():
        return {"ok": True, "simulated": True}

    @app.post("/webhook")
    async def webhook(request: Request):
        if not settings.webhook_secret:
            raise HTTPException(503, "webhook locked: set WEBHOOK_SECRET on the server")
        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            raise HTTPException(413, "body too large")
        # TradingView sends text/plain unless the message is valid JSON, so parse it ourselves.
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, "body must be JSON")
        if not isinstance(data, dict):
            raise HTTPException(400, "body must be a JSON object")
        given = data.pop("secret", None)
        if not isinstance(given, str) or not _same(given, settings.webhook_secret):
            log.warning("webhook rejected: bad secret")
            raise HTTPException(401, "bad secret")
        try:
            sig = Signal.model_validate(data)
        except ValidationError as e:
            raise HTTPException(422, json.loads(e.json(include_url=False, include_input=False)))
        result = sim.process(sig, "webhook")
        log.info("webhook %s %s @ %s -> %s", sig.action, sig.symbol, sig.price, result.status)
        return result.to_dict()

    @app.get("/api/state", dependencies=[Depends(require_token)])
    def state(limit: int = 200):
        return sim.state(order_limit=max(1, min(limit, 1000)))

    @app.post("/api/test/signal", dependencies=[Depends(require_token)])
    def test_signal(sig: Signal):
        if not settings.test_mode:
            raise HTTPException(403, "test mode is off (TEST_MODE=false)")
        if not sig.id:
            # Each button press is a new signal; without this, repeats look like duplicates.
            sig = sig.model_copy(update={"id": "test-" + secrets.token_hex(8)})
        return sim.process(sig, "test").to_dict()

    @app.post("/api/positions/stop", dependencies=[Depends(require_token)])
    def update_stop(body: StopUpdate):
        if not sim.set_stop(body.symbol, body.stop_price):
            raise HTTPException(404, "no open position for that symbol")
        return {"ok": True}

    @app.post("/api/reset", dependencies=[Depends(require_token)])
    def reset(body: ResetRequest):
        if body.confirm != "RESET":
            raise HTTPException(400, 'send {"confirm": "RESET"} to really start over')
        sim.reset()
        return {"ok": True, "cash": settings.starting_balance}

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException):
        return JSONResponse({"simulated": True, "error": exc.detail}, status_code=exc.status_code)

    return app


app = create_app()
