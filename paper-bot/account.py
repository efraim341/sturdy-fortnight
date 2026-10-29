"""The virtual account Claude trades on the user's behalf, under the user's iron rules.

Uses the same simulator as the webhook server, with the account kept in
account/paperbot.db (committed to git so it survives between sessions).
Prices come from Yahoo's free chart endpoint. There is no broker anywhere:
every order is a row in the local database.

    python3 account.py state                       # balance, positions, P&L
    python3 account.py check                       # live prices -> enforce the -3% stop
    python3 account.py buy NVDA --score 8.5 --why "..."   # 5% of total value, 3% stop
    python3 account.py sell NVDA --why "..."
    python3 account.py quote NVDA AAPL

Iron rules (see CLAUDE.md), enforced here, not just promised:
    - buy only with a score of 8 or more
    - each buy is at most 5% of the account's total value, fee included
    - every position has a stop 3% under its buy price
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.config import load_settings  # noqa: E402
from app.simulator import Signal, Simulator  # noqa: E402
from app.storage import Store  # noqa: E402

DB = ROOT / "account" / "paperbot.db"
JOURNAL = ROOT / "account" / "journal.jsonl"

MIN_SCORE = 8.0
MAX_TRADE_PCT = 5.0
STOP_PCT = 3.0

UA = "Mozilla/5.0 (compatible; paperbot/1.0)"


def simulator() -> Simulator:
    s = replace(
        load_settings(),
        db_path=str(DB),
        starting_balance=10_000.0,
        fee_rate=0.001,
        fee_fixed=0.0,
        default_stop_loss_pct=STOP_PCT,
        max_daily_loss_usd=0.0,  # not one of the user's rules
        max_order_usd=1e12,      # the 5% rule is applied per buy below
        allow_fractional=True,   # fractional shares, so 5% can buy any stock
        dedup_window_seconds=1,
    )
    return Simulator(s, Store(str(DB)))


def quote(symbol: str) -> float:
    """Latest price from Yahoo (regular session, or the last close when the market is shut)."""
    last_err = None
    # Yahoo rate-limits (HTTP 429) shared cloud IPs: alternate hosts and back off.
    for attempt in range(8):
        host = ("query1", "query2")[attempt % 2]
        url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}?range=1d&interval=1m"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=15) as r:
                meta = json.load(r)["chart"]["result"][0]["meta"]
            return float(meta["regularMarketPrice"])
        except Exception as e:
            last_err = e
            time.sleep(min(60, 5 * (attempt + 1)))
    raise SystemExit(f"no price for {symbol}: {last_err}")


def journal(entry: dict) -> None:
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    with JOURNAL.open("a") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()), **entry}) + "\n")


def show(sim: Simulator) -> None:
    st = sim.state(order_limit=10)
    print(f"total value ${st['equity']:,.2f} | cash ${st['cash']:,.2f} | "
          f"P&L {st['total_pnl']:+,.2f} (realized {st['realized_pnl']:+,.2f}, fees {st['fees_paid']:,.2f})")
    for p in st["positions"]:
        chg = (p["last_price"] / p["avg_price"] - 1) * 100
        print(f"  {p['symbol']:6} {p['qty']:g} @ {p['avg_price']:.2f} -> {p['last_price']:.2f} "
              f"({chg:+.2f}%, {p['unrealized_pnl']:+,.2f}) stop {p['stop_price']:.2f}")
    if not st["positions"]:
        print("  no open positions")


def cmd_check(sim: Simulator) -> None:
    for p in sim.state()["positions"]:
        price = quote(p["symbol"])
        r = sim.process(Signal(action="price", symbol=p["symbol"], price=price), "test")
        for o in r.orders:
            print(f"STOP: sold {o['qty']:g} {o['symbol']} @ {o['price']:.2f} ({o['realized_pnl']:+,.2f}) - {o['reason']}")
            journal({"action": "stop_loss", "symbol": o["symbol"], "price": o["price"], "pnl": o["realized_pnl"]})
    show(sim)


def cmd_buy(sim: Simulator, symbol: str, score: float, why: str) -> None:
    if score < MIN_SCORE:
        raise SystemExit(f"REFUSED: score {score} is under {MIN_SCORE:g} (iron rule)")
    symbol = symbol.upper()
    if any(p["symbol"] == symbol for p in sim.state()["positions"]):
        raise SystemExit(f"REFUSED: already holding {symbol}; adding would break the 5% per-trade cap")
    price = quote(symbol)
    sim.process(Signal(action="price", symbol=symbol, price=price), "test")  # mark positions first
    equity = sim.state()["equity"]
    budget = equity * MAX_TRADE_PCT / 100 / (1 + sim.s.fee_rate)  # fee included in the 5%
    qty = int(budget / price * 1e6) / 1e6
    if qty <= 0:
        raise SystemExit(f"REFUSED: no budget left for {symbol} under the 5% rule")
    r = sim.process(Signal(action="buy", symbol=symbol, price=price, qty=qty, stop_loss_pct=STOP_PCT), "test")
    o = r.orders[-1]
    print(f"{r.status.upper()}: {o['action']} {o['qty']:g} {symbol} @ {price:.2f} "
          f"= ${o['qty'] * price:,.2f} + fee {o['fee']:.2f} | {r.message}")
    if r.status == "filled":
        journal({"action": "buy", "symbol": symbol, "price": price, "qty": qty, "score": score, "why": why})
    show(sim)


def cmd_sell(sim: Simulator, symbol: str, why: str) -> None:
    symbol = symbol.upper()
    price = quote(symbol)
    r = sim.process(Signal(action="close", symbol=symbol, price=price), "test")
    for o in r.orders:
        print(f"{o['status'].upper()}: {o['action']} {o['qty']:g} {symbol} @ {o['price']:.2f} "
              f"({o['realized_pnl']:+,.2f}) - {o['reason']}")
    if r.status == "filled":
        journal({"action": "sell", "symbol": symbol, "price": price, "why": why})
    show(sim)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("state")
    sub.add_parser("check")
    b = sub.add_parser("buy")
    b.add_argument("symbol")
    b.add_argument("--score", type=float, required=True)
    b.add_argument("--why", required=True)
    s = sub.add_parser("sell")
    s.add_argument("symbol")
    s.add_argument("--why", required=True)
    q = sub.add_parser("quote")
    q.add_argument("symbols", nargs="+")
    a = ap.parse_args()

    if a.cmd == "quote":
        for sym in a.symbols:
            print(f"{sym.upper()} {quote(sym.upper()):.2f}")
        return
    sim = simulator()
    {"state": lambda: show(sim), "check": lambda: cmd_check(sim),
     "buy": lambda: cmd_buy(sim, a.symbol, a.score, a.why),
     "sell": lambda: cmd_sell(sim, a.symbol, a.why)}[a.cmd]()


if __name__ == "__main__":
    main()
