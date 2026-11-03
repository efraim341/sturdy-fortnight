"""Price data for research.py and account.py: Twelve Data first, Yahoo as a fallback.

Twelve Data (api.twelvedata.com) is a market-data API with a free personal plan:
800 requests a day, 8 a minute. The API key never appears here: the cloud
environment's network secret adds the "Authorization: apikey ..." header to every
request to that host. Without the secret (or when Twelve Data has no data for a
symbol) the Yahoo chart endpoint is tried instead.

All prices are regular-session prices in US dollars. Bars come oldest first.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

TD = "https://api.twelvedata.com"
UA = {"User-Agent": "Mozilla/5.0 (compatible; paperbot/1.0)"}
NY = ZoneInfo("America/New_York")
PER_MINUTE = 8  # Twelve Data free plan

_recent: deque[float] = deque()


class NoData(Exception):
    pass


def _http(url: str, timeout: int = 30) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


def _twelve(path: str, **params) -> dict:
    """One Twelve Data call, kept under the per-minute limit; waits out a 429."""
    url = f"{TD}/{path}?{urllib.parse.urlencode(params)}"
    for attempt in range(4):
        while len(_recent) >= PER_MINUTE and time.time() - _recent[0] < 61:
            time.sleep(61 - (time.time() - _recent[0]))
        while _recent and time.time() - _recent[0] >= 61:
            _recent.popleft()
        _recent.append(time.time())
        try:
            data = json.loads(_http(url))
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = str(e)
            time.sleep(5 * (attempt + 1))
            continue
        if data.get("status") == "error":
            last = f"{data.get('code')}: {data.get('message', '')[:120]}"
            if data.get("code") == 429:  # minute or daily credits used up
                time.sleep(61)
                continue
            raise NoData(last)
        return data
    raise NoData(f"Twelve Data: {last}")


def _ts(dt: str) -> float:
    fmt = "%Y-%m-%d %H:%M:%S" if " " in dt else "%Y-%m-%d"
    return datetime.strptime(dt, fmt).replace(tzinfo=NY).timestamp()


def _td_bars(symbol: str, interval: str, **params) -> list[tuple]:
    data = _twelve("time_series", symbol=symbol, interval=interval, order="asc", **params)
    return [(_ts(v["datetime"]), float(v["open"]), float(v["high"]), float(v["low"]), float(v["close"]))
            for v in data.get("values", [])]


def _yahoo(symbol: str, query: str) -> dict:
    last = None
    for attempt in range(4):
        host = ("query1", "query2")[attempt % 2]
        try:
            raw = _http(f"https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}?{query}", 15)
            return json.loads(raw)["chart"]["result"][0]
        except Exception as e:
            last = e
            time.sleep(5 * (attempt + 1))
    raise NoData(f"Yahoo: {last}")


def _yahoo_bars(symbol: str, query: str) -> list[tuple]:
    r = _yahoo(symbol, query)
    q = r["indicators"]["quote"][0]
    return [(t, o, h, l, c) for t, o, h, l, c in
            zip(r.get("timestamp") or [], q["open"], q["high"], q["low"], q["close"]) if o and h and l and c]


def daily(symbol: str, days: int = 520) -> list[tuple]:
    """(ts, open, high, low, close) daily bars, about two years."""
    try:
        return _td_bars(symbol, "1day", outputsize=days)
    except NoData:
        return _yahoo_bars(symbol, "range=2y&interval=1d")


def price(symbol: str) -> float:
    """Latest price (or the last close when the market is shut)."""
    try:
        return float(_twelve("price", symbol=symbol)["price"])
    except NoData:
        return float(_yahoo(symbol, "range=1d&interval=1m")["meta"]["regularMarketPrice"])


def bars_since(symbol: str, since: float) -> list[tuple]:
    """(ts, open, high, low) regular-session bars that began after `since`, oldest first:
    5-minute bars for roughly the last two months, hourly bars beyond that."""
    recent = time.time() - since < 55 * 86_400
    start = datetime.fromtimestamp(since, NY).strftime("%Y-%m-%d %H:%M:%S")
    try:
        rows = _td_bars(symbol, "5min" if recent else "1h", start_date=start, outputsize=5000)
    except NoData:
        rows = _yahoo_bars(symbol, f"period1={int(since)}&period2={int(time.time())}"
                                   f"&interval={'5m' if recent else '60m'}")
    return [(t, o, h, l) for t, o, h, l, _ in rows if t > since]
