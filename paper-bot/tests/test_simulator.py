from dataclasses import replace

import pytest

from app.simulator import Signal, Simulator
from app.storage import Store

NOW = 1_790_000_000.0  # a fixed moment, so "today" is stable


def sim(settings, **over):
    return Simulator(replace(settings, **over), Store(":memory:"))


def sig(**kw):
    kw.setdefault("symbol", "AAPL")
    return Signal(**kw)


def test_starts_with_10000(settings):
    s = sim(settings).state(now=NOW)
    assert s["cash"] == 10_000 and s["equity"] == 10_000 and s["total_pnl"] == 0
    assert s["simulated"] is True


def test_buy_charges_value_plus_fee_and_sets_default_stop(settings):
    sm = sim(settings)
    r = sm.process(sig(action="buy", price=100, qty=10, id="a"), "test", NOW)
    assert r.status == "filled"
    st = sm.state(now=NOW)
    assert st["cash"] == pytest.approx(10_000 - 1_000 - 1.0)
    pos = st["positions"][0]
    assert pos["qty"] == 10
    assert pos["avg_price"] == pytest.approx(100.1)  # fee folded into cost
    assert pos["stop_price"] == pytest.approx(98.0)


def test_buy_with_usd_amount_and_explicit_stop(settings):
    sm = sim(settings)
    sm.process(sig(action="buy", price=50, usd=500, stop_loss=45, id="a"), "test", NOW)
    pos = sm.state(now=NOW)["positions"][0]
    assert pos["qty"] == 10 and pos["stop_price"] == 45


def test_whole_shares_only(settings):
    sm = sim(settings, allow_fractional=False)
    sm.process(sig(action="buy", price=300, usd=1000, id="a"), "test", NOW)
    assert sm.state(now=NOW)["positions"][0]["qty"] == 3


def test_round_trip_pnl_is_net_of_both_fees(settings):
    sm = sim(settings)
    sm.process(sig(action="buy", price=100, qty=10, id="a"), "test", NOW)
    r = sm.process(sig(action="sell", price=110, id="b"), "test", NOW)
    o = r.orders[0]
    # bought 1000 + 1.0 fee, sold 1100 - 1.1 fee
    assert o["realized_pnl"] == pytest.approx(1100 - 1.1 - 1001)
    st = sm.state(now=NOW)
    assert st["positions"] == []
    assert st["cash"] == pytest.approx(10_000 + 1100 - 1.1 - 1001)
    assert st["fees_paid"] == pytest.approx(2.1)


def test_partial_sell(settings):
    sm = sim(settings)
    sm.process(sig(action="buy", price=100, qty=10, id="a"), "test", NOW)
    sm.process(sig(action="sell", price=100, qty=4, id="b"), "test", NOW)
    assert sm.state(now=NOW)["positions"][0]["qty"] == 6


def test_sell_without_position_is_rejected_no_shorting(settings):
    sm = sim(settings)
    r = sm.process(sig(action="sell", price=100, id="a"), "test", NOW)
    assert r.status == "rejected" and "no open position" in r.message
    assert sm.state(now=NOW)["cash"] == 10_000


def test_order_size_limit(settings):
    sm = sim(settings)
    r = sm.process(sig(action="buy", price=100, qty=25, id="a"), "test", NOW)  # $2,500 > $2,000
    assert r.status == "rejected" and "limit" in r.message
    assert sm.state(now=NOW)["positions"] == []


def test_not_enough_cash(settings):
    sm = sim(settings, starting_balance=500.0)
    r = sm.process(sig(action="buy", price=100, qty=10, id="a"), "test", NOW)
    assert r.status == "rejected" and "cash" in r.message


def test_stop_loss_triggers_on_price_update(settings):
    sm = sim(settings)
    sm.process(sig(action="buy", price=100, qty=10, stop_loss_pct=5, id="a"), "test", NOW)
    r = sm.process(sig(action="price", price=96, id="b"), "test", NOW)
    assert r.status == "ok" and sm.state(now=NOW)["positions"]
    r = sm.process(sig(action="price", price=94, id="c"), "test", NOW)
    assert r.status == "filled"
    assert r.orders[0]["source"] == "stop_loss" and r.orders[0]["price"] == 94
    assert sm.state(now=NOW)["positions"] == []


def test_stop_checked_before_a_new_signal(settings):
    sm = sim(settings)
    sm.process(sig(action="buy", price=100, qty=10, id="a"), "test", NOW)
    r = sm.process(sig(action="buy", price=90, qty=1, id="b"), "test", NOW)
    assert r.orders[0]["source"] == "stop_loss"
    assert r.orders[1]["action"] == "buy" and r.orders[1]["status"] == "filled"


def test_stop_must_be_below_price(settings):
    r = sim(settings).process(sig(action="buy", price=100, qty=1, stop_loss=101, id="a"), "test", NOW)
    assert r.status == "rejected"


def test_daily_loss_limit_blocks_buys_but_not_sells(settings):
    sm = sim(settings, max_order_usd=10_000)
    sm.process(sig(action="buy", price=100, qty=20, stop_loss_pct=0, id="a"), "test", NOW)
    sm.process(sig(action="buy", price=10, qty=10, symbol="MSFT", stop_loss_pct=0, id="m"), "test", NOW)
    sm.process(sig(action="sell", price=80, id="b"), "test", NOW)  # lose ~$400
    st = sm.state(now=NOW)
    assert st["today_realized_pnl"] < -300 and st["buys_blocked_today"]
    r = sm.process(sig(action="buy", price=80, qty=1, id="c"), "test", NOW)
    assert r.status == "rejected" and "daily loss" in r.message
    r = sm.process(sig(action="sell", price=10, symbol="MSFT", id="d"), "test", NOW)
    assert r.status == "filled"
    # next day buys work again
    r = sm.process(sig(action="buy", price=80, qty=1, id="e"), "test", NOW + 86_400)
    assert r.status == "filled"


def test_duplicate_signal_ignored(settings):
    sm = sim(settings)
    assert sm.process(sig(action="buy", price=100, qty=1, id="x"), "test", NOW).status == "filled"
    assert sm.process(sig(action="buy", price=100, qty=1, id="x"), "test", NOW).status == "duplicate"
    # without an id, identical content is a duplicate too
    assert sm.process(sig(action="buy", price=100, qty=1), "test", NOW).status == "filled"
    assert sm.process(sig(action="buy", price=100, qty=1), "test", NOW).status == "duplicate"
    assert sm.state(now=NOW)["positions"][0]["qty"] == 2


def test_duplicate_window_expires(settings):
    sm = sim(settings, dedup_window_seconds=60)
    sm.process(sig(action="buy", price=100, qty=1, id="x"), "test", NOW)
    assert sm.process(sig(action="buy", price=100, qty=1, id="x"), "test", NOW + 61).status == "filled"


def test_reset(settings):
    sm = sim(settings)
    sm.process(sig(action="buy", price=100, qty=1, id="x"), "test", NOW)
    sm.reset()
    st = sm.state(now=NOW)
    assert st["cash"] == 10_000 and st["positions"] == [] and st["orders"] == []


def test_symbol_cleanup():
    assert sig(action="buy", price=1, symbol="binance:btcusdt").symbol == "BTCUSDT"
    with pytest.raises(ValueError):
        sig(action="buy", price=1, symbol="AA PL;DROP")
