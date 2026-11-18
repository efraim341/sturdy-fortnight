"""account.py's trailing-stop replay (rule 4: 10% under the peak), prices stubbed (no network)."""

import pytest

import account


def setup(tmp_path, monkeypatch, price=100.0):
    monkeypatch.setattr(account, "DB", tmp_path / "a.db")
    monkeypatch.setattr(account, "JOURNAL", tmp_path / "j.jsonl")
    monkeypatch.setattr(account, "quote", lambda s: price)
    sim = account.simulator()
    account.cmd_buy(sim, "XYZ", 8.5, "test")
    return sim, sim.state()["positions"][0]["opened_at"]


def test_buy_starts_the_stop_10pct_under(tmp_path, monkeypatch):
    sim, _ = setup(tmp_path, monkeypatch)
    assert abs(sim.state()["positions"][0]["stop_price"] - 90.0) < 1e-6


def test_peak_raises_stop_and_hit_sells_at_stop(tmp_path, monkeypatch):
    sim, t0 = setup(tmp_path, monkeypatch)
    bars = [(t0 + 300, 100, 120, 99), (t0 + 600, 115, 116, 107)]  # peak 120 -> stop 108
    monkeypatch.setattr(account, "bars_since", lambda s, since: bars)
    account.cmd_check(sim)
    st = sim.state()
    assert st["positions"] == []
    assert st["orders"][0]["source"] == "stop_loss" and st["orders"][0]["price"] == 108.0


def test_dip_before_the_peak_never_triggers_the_raised_stop(tmp_path, monkeypatch):
    sim, t0 = setup(tmp_path, monkeypatch)
    bars = [(t0 + 300, 100, 100.5, 92.0), (t0 + 600, 99, 110, 99)]  # dip to 92, then peak 110
    monkeypatch.setattr(account, "bars_since", lambda s, since: bars)
    monkeypatch.setattr(account, "quote", lambda s: 109.0)
    account.cmd_check(sim)
    account.cmd_check(sim)  # replays the same bars with the stop already at 99
    pos = sim.state()["positions"]
    assert pos and abs(pos[0]["stop_price"] - 99.0) < 1e-6


def test_under_8_needs_the_users_decision(tmp_path, monkeypatch):
    sim, _ = setup(tmp_path, monkeypatch)
    with pytest.raises(SystemExit):
        account.cmd_buy(sim, "ABC", 7.5, "test")
    account.cmd_buy(sim, "ABC", 7.5, "user said so", user_decided=True)
    assert {p["symbol"] for p in sim.state()["positions"]} == {"XYZ", "ABC"}
