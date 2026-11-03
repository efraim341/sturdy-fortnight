"""The virtual account Claude trades on the user's behalf, under the user's iron rules.

Uses the same simulator as the webhook server, with the account kept in
account/paperbot.db (committed to git so it survives between sessions).
Prices come from marketdata.py (Twelve Data, Yahoo as fallback). There is no broker anywhere:
every order is a row in the local database.

    python3 account.py state                       # balance, positions, P&L
    python3 account.py report                      # end-of-day summary per stock
    python3 account.py check                       # replay bars since each buy -> trailing -3% stop
    python3 account.py buy NVDA --score 8.5 --why "..."   # 5% of total value, 3% stop
    python3 account.py sell NVDA --why "..."
    python3 account.py quote NVDA AAPL

Iron rules (see CLAUDE.md), enforced here, not just promised:
    - buy only with a score of 8 or more, unless the user decides in chat to buy an
      under-8 stock anyway (--user-decided); the 5% cap and the stop still apply
    - each buy is at most 5% of the account's total value, fee included
    - every position has a trailing stop: 3% under the highest price since the buy
      (it starts 3% under the buy price and only moves up)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.config import load_settings  # noqa: E402
from app.simulator import Signal, Simulator  # noqa: E402
from app.storage import Store  # noqa: E402
import marketdata  # noqa: E402

DB = ROOT / "account" / "paperbot.db"
JOURNAL = ROOT / "account" / "journal.jsonl"

MIN_SCORE = 8.0
MAX_TRADE_PCT = 5.0
STOP_PCT = 3.0

def simulator() -> Simulator:
    s = replace(
        load_settings(),
        db_path=str(DB),
        starting_balance=10_000.0,
        fee_rate=0.001,
        fee_fixed=0.0,
        default_stop_loss_pct=STOP_PCT,
        trailing_stop_pct=STOP_PCT,  # rule 4: the stop trails 3% under the highest price
        max_daily_loss_usd=0.0,  # not one of the user's rules
        max_order_usd=1e12,      # the 5% rule is applied per buy below
        allow_fractional=True,   # fractional shares, so 5% can buy any stock
        dedup_window_seconds=1,
    )
    return Simulator(s, Store(str(DB)))


def quote(symbol: str) -> float:
    """Latest price (regular session, or the last close when the market is shut)."""
    try:
        return marketdata.price(symbol)
    except Exception as e:
        raise SystemExit(f"no price for {symbol}: {e}")


def bars_since(symbol: str, since: float) -> list[tuple]:
    """(ts, open, high, low) of every regular-session bar after `since`, oldest first."""
    try:
        return marketdata.bars_since(symbol, since)
    except Exception as e:
        raise SystemExit(f"no bars for {symbol}: {e}")


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


def report(sim: Simulator) -> None:
    """End-of-day summary: every stock bought, its buy price, its trailing stop now, its P&L."""
    st = sim.state(order_limit=100_000)
    filled = [o for o in reversed(st["orders"]) if o["status"] == "filled"]
    print(f"Account: total value ${st['equity']:,.2f} | cash ${st['cash']:,.2f} | "
          f"P&L {st['total_pnl']:+,.2f} ({st['total_pnl'] / st['starting_balance']:+.2%}) since "
          f"${st['starting_balance']:,.0f} | fees paid ${st['fees_paid']:,.2f}")
    print("\nOPEN (P&L includes the buy fee):")
    for p in st["positions"]:
        buy = next(o for o in reversed(filled) if o["symbol"] == p["symbol"] and o["action"] == "buy")
        peak = max(buy["price"], p["stop_price"] / (1 - STOP_PCT / 100))
        cost = p["qty"] * p["avg_price"]
        print(f"  {p['symbol']:6} bought {buy['price']:.2f} on {time.strftime('%Y-%m-%d', time.gmtime(buy['ts']))} | "
              f"now {p['last_price']:.2f} | peak {peak:.2f} | stop {p['stop_price']:.2f} (peak x 0.97) | "
              f"P&L {p['unrealized_pnl']:+,.2f} ({p['unrealized_pnl'] / cost:+.2%}) on ${cost:,.2f}")
    if not st["positions"]:
        print("  none")
    print("\nCLOSED (P&L after both fees):")
    sells = [o for o in filled if o["action"] == "sell"]
    for o in sells:
        buy = next(b for b in reversed(filled) if b["symbol"] == o["symbol"] and b["action"] == "buy"
                   and b["ts"] <= o["ts"])
        why = "trailing stop" if o["source"] == "stop_loss" else "sold"
        print(f"  {o['symbol']:6} bought {buy['price']:.2f} -> {why} {o['price']:.2f} on "
              f"{time.strftime('%Y-%m-%d', time.gmtime(o['ts']))} | P&L {o['realized_pnl']:+,.2f}")
    if not sells:
        print("  none")


def price_signal(sim: Simulator, symbol: str, price: float, ts: float | None = None) -> list[dict]:
    sig = Signal(action="price", symbol=symbol, price=price, id=uuid.uuid4().hex)
    return sim.process(sig, "test", now=ts).orders


def replay(sim: Simulator, p: dict) -> list[dict]:
    """Walk every bar since the buy, as a real trailing stop would have seen it.

    Within a bar the order of high and low is unknown, so take the cautious one:
    the low is tested against the stop from before the bar, then the bar's high
    raises the stop. A bar that opens under the stop fills at its open (a gap).
    """
    stop = p["stop_price"]
    for ts, o, h, l in bars_since(p["symbol"], p["opened_at"]):
        if l <= stop:
            sim.set_stop(p["symbol"], stop)
            return price_signal(sim, p["symbol"], min(o, stop), ts)
        stop = max(stop, round(h * (1 - STOP_PCT / 100), 6))
    sim.set_stop(p["symbol"], stop)
    return []


def cmd_check(sim: Simulator) -> None:
    """Rule 4: the stop trails 3% under the highest price since the buy; sell when it is hit."""
    for p in sim.state()["positions"]:
        sold = replay(sim, p) or price_signal(sim, p["symbol"], quote(p["symbol"]))
        for o in sold:
            when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(o["ts"]))
            print(f"STOP: sold {o['qty']:g} {o['symbol']} @ {o['price']:.2f} on {when} "
                  f"({o['realized_pnl']:+,.2f}) - {o['reason']}")
            journal({"action": "stop_loss", "symbol": o["symbol"], "price": o["price"],
                     "pnl": o["realized_pnl"], "at": when})
    show(sim)


def cmd_buy(sim: Simulator, symbol: str, score: float, why: str, user_decided: bool = False) -> None:
    # Rule 2: under 8 only when the user decided in chat to buy this stock anyway.
    if score < MIN_SCORE and not user_decided:
        raise SystemExit(f"REFUSED: score {score} is under {MIN_SCORE:g} (iron rule); "
                         "only the user can decide to buy it anyway (--user-decided)")
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
        journal({"action": "buy", "symbol": symbol, "price": price, "qty": qty, "score": score, "why": why,
                 **({"user_decided": True} if score < MIN_SCORE else {})})
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
    sub.add_parser("report")
    sub.add_parser("check")
    b = sub.add_parser("buy")
    b.add_argument("symbol")
    b.add_argument("--score", type=float, required=True)
    b.add_argument("--why", required=True)
    b.add_argument("--user-decided", action="store_true",
                   help="the user explicitly decided in chat to buy this under-8 stock (rule 2)")
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
    {"state": lambda: show(sim), "report": lambda: report(sim), "check": lambda: cmd_check(sim),
     "buy": lambda: cmd_buy(sim, a.symbol, a.score, a.why, a.user_decided),
     "sell": lambda: cmd_sell(sim, a.symbol, a.why)}[a.cmd]()


if __name__ == "__main__":
    main()
