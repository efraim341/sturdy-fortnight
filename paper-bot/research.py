"""Deep check of the scanner's candidates, for the user's 1-10 scores.

Reads trader/watchlist.json (from `trader-scanner daily`), keeps the bullish setups
(20-day breakout, bull flag, reclaimed 20-day average), and for the most traded ones
measures what the iron rules care about:

- trend and strength: 1/3/6/12-month returns, vs the S&P 500 (SPY), above rising 50/200-day averages
- safety under a -3% stop: volatility, ATR, share of days that dipped 3%
- stop test: over the past year, how often a buy hit -3% before +6% (break-even 33%),
  and how often the stop was gapped through (a loss bigger than 3%)
- upcoming earnings: Yahoo headlines that announce a report (gap risk past the stop)

It prints a ranked shortlist with a mechanical pre-score. The pre-score is only a
starting point: the final 1-10 score comes after news and SEC filings are read.

    python3 research.py [--watchlist trader/watchlist.json] [--top 80] [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics as st
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

UA = {"User-Agent": "Mozilla/5.0 (compatible; paperbot/1.0)"}
BULLISH = {"breakout_20d", "bull_flag", "reclaim_ma20"}
PAUSE = 0.5


def get(url: str, rounds: int = 7) -> bytes:
    """GET with patience: Yahoo answers 429 to busy cloud IPs, so wait and retry."""
    last = None
    for k in range(rounds):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code != 429 and e.code < 500:
                raise
        except (urllib.error.URLError, OSError) as e:
            last = e
        time.sleep(5 * (k + 1))
        url = url.replace("query1.", "query2.") if "query1." in url else url.replace("query2.", "query1.")
    raise RuntimeError(f"gave up after {rounds} tries: {last}")


def daily(symbol: str):
    raw = get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=2y&interval=1d")
    r = json.loads(raw)["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    rows = [(o, h, l, c) for o, h, l, c in zip(q["open"], q["high"], q["low"], q["close"])
            if o and h and l and c]
    return rows, r["meta"]


def earnings_soon(symbol: str) -> str:
    """Best effort: a recent headline that says a report is coming (empty if none or unknown)."""
    try:
        xml = get(f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US", rounds=2)
        titles = [i.findtext("title") or "" for i in ET.fromstring(xml).iter("item")]
    except Exception:
        return "unknown"
    ahead = re.compile(r"(to report|ahead of|reports next|earnings preview|what to expect|"
                       r"expected to|will .* beat|earnings (tomorrow|this week|next week)|"
                       r"set to report|before (its|the) (q\d|earnings))", re.I)
    done = re.compile(r"(q\d earnings snapshot|reports q\d|beats|misses|surpasses|tops|"
                      r"earnings call (transcript|highlights|summary))", re.I)
    for t in titles[:25]:
        if done.search(t):
            return ""  # already reported this season
        if ahead.search(t):
            return t[:90]
    return ""


def metrics(sym: str, rows, spy_close):
    c = [r[3] for r in rows]
    if len(c) < 260:
        return None
    ret = lambda xs, n: xs[-1] / xs[-1 - n] - 1
    sma = lambda n, end=None: sum(c[(end or len(c)) - n:(end or len(c))]) / n
    d = [c[i] / c[i - 1] - 1 for i in range(len(c) - 60, len(c))]
    atr = sum(max(rows[i][1] - rows[i][2], abs(rows[i][1] - c[i - 1]), abs(rows[i][2] - c[i - 1]))
              for i in range(len(c) - 14, len(c))) / 14 / c[-1]
    dip3 = sum(1 for i in range(len(c) - 60, len(c)) if rows[i][2] < c[i - 1] * 0.97) / 60
    # stop test: enter at each close of the past year, -3% stop vs +6% target, 20 days max
    win = lose = gapped = 0
    for i in range(len(c) - 260, len(c) - 1):
        e = c[i]
        for j in range(i + 1, min(i + 21, len(rows))):
            o, h, l, _ = rows[j]
            if l <= e * 0.97:
                lose += 1
                gapped += o < e * 0.97
                break
            if h >= e * 1.06:
                win += 1
                break
    m = {
        "price": round(c[-1], 2),
        "r1m": ret(c, 21), "r3m": ret(c, 63), "r6m": ret(c, 126), "r1y": ret(c, 252),
        "rs3m": ret(c, 63) - ret(spy_close, 63),
        "above50": c[-1] > sma(50), "above200": c[-1] > sma(200),
        "sma50_rising": sma(50) > sma(50, len(c) - 10),
        "vol": st.pstdev(d) * math.sqrt(252), "atr": atr, "dip3": dip3,
        "off_high": c[-1] / max(c[-252:]) - 1,
        "stop_test_win": win / max(1, win + lose), "stop_test_n": win + lose,
        "gap_through": gapped / max(1, lose),
    }
    # mechanical pre-score, 0-10 (the final score also weighs news, filings and earnings timing)
    s = 0.0
    s += min(2.0, max(0.0, m["rs3m"] * 8))                     # strength vs market
    s += 0.5 * m["above50"] + 0.5 * m["above200"] + 0.5 * m["sma50_rising"]
    s += max(0.0, 1.5 - m["vol"] * 3)                           # calm enough for a 3% stop
    s += max(0.0, 1.0 - m["dip3"] * 5)
    s += min(2.5, max(0.0, (m["stop_test_win"] - 0.33) * 15))   # edge over break-even
    s += 0.5 if m["off_high"] > -0.05 else 0.0
    s -= min(1.0, max(0.0, m["gap_through"] - 0.15) * 4)
    m["pre_score"] = round(min(10.0, s), 1)
    return m


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--watchlist", default="trader/watchlist.json")
    ap.add_argument("--top", type=int, default=80, help="how many bullish hits to check, most traded first")
    ap.add_argument("--min-price", type=float, default=10)
    ap.add_argument("--json")
    a = ap.parse_args()

    wl = json.load(open(a.watchlist))
    hits = [h for h in wl["hits"] if BULLISH & set(h.get("setups", [])) and h.get("price", 0) >= a.min_price]
    hits = hits[: a.top]
    spy, _ = daily("SPY")
    spy_close = [r[3] for r in spy]
    out, failed = [], []
    for n, h in enumerate(hits, 1):
        try:
            rows, meta = daily(h["symbol"])
            m = metrics(h["symbol"], rows, spy_close)
        except Exception as e:
            failed.append(f"{h['symbol']} ({e})")
            continue
        if m:
            m.update(symbol=h["symbol"], name=(meta.get("longName") or meta.get("shortName") or "")[:30],
                     setups=",".join(h["setups"]), rsi=h.get("rsi"))
            out.append(m)
        if n % 20 == 0:
            print(f"  checked {n} of {len(hits)}", file=sys.stderr)
        time.sleep(PAUSE)
    out.sort(key=lambda m: -m["pre_score"])
    for m in out[:20]:
        m["earnings_soon"] = earnings_soon(m["symbol"])
        time.sleep(PAUSE)

    print(f"Checked {len(out)} of {len(hits)} bullish candidates (failed: {len(failed)}).")
    print("pre  sym    price    rsi  3m    vsSPY 1y    vol  dip3 stop-test(n) gap  offHi  earnings")
    for m in out[:25]:
        print(f"{m['pre_score']:>4} {m['symbol']:6}{m['price']:>8} {m['rsi'] or 0:5.1f} {m['r3m']:+5.0%} "
              f"{m['rs3m']:+5.0%} {m['r1y']:+5.0%} {m['vol']:4.0%} {m['dip3']:4.0%} "
              f"{m['stop_test_win']:4.0%}({m['stop_test_n']}) {m['gap_through']:4.0%} {m['off_high']:+5.0%}  "
              f"{m.get('earnings_soon', '')[:60]}")
    if failed:
        print("Failed:", ", ".join(failed[:20]))
    if a.json:
        json.dump({"checked": len(out), "failed": failed, "candidates": out}, open(a.json, "w"), indent=1)


if __name__ == "__main__":
    main()
