"""account.py's trailing-stop replay, with prices stubbed out (no network)."""

import account


def setup(tmp_path, monkeypatch, price=100.0):
    monkeypatch.setattr(account, "DB", tmp_path / "a.db")
    monkeypatch.setattr(account, "JOURNAL", tmp_path / "j.jsonl")
    monkeypatch.setattr(account, "quote", lambda s: price)
    sim = account.simulator()
    account.cmd_buy(sim, "XYZ", 8.5, "test")
    return sim, sim.state()["positions"][0]["opened_at"]


def test_peak_raises_stop_and_hit_sells_at_stop(tmp_path, monkeypatch):
    sim, t0 = setup(tmp_path, monkeypatch)
    bars = [(t0 + 300, 100, 120, 99), (t0 + 600, 118, 118, 115)]
    monkeypatch.setattr(account, "bars_since", lambda s, since: bars)
    account.cmd_check(sim)
    st = sim.state()
    assert st["positions"] == []
    assert st["orders"][0]["source"] == "stop_loss" and st["orders"][0]["price"] == 116.4


def test_dip_before_the_peak_never_triggers_the_raised_stop(tmp_path, monkeypatch):
    sim, t0 = setup(tmp_path, monkeypatch)
    bars = [(t0 + 300, 100, 100.5, 98.0), (t0 + 600, 99, 110, 99)]
    monkeypatch.setattr(account, "bars_since", lambda s, since: bars)
    monkeypatch.setattr(account, "quote", lambda s: 109.0)
    account.cmd_check(sim)
    account.cmd_check(sim)  # a second check replays the same bars with the stop already at 106.7
    pos = sim.state()["positions"]
    assert pos and abs(pos[0]["stop_price"] - 106.7) < 1e-6


def test_under_8_needs_the_users_decision(tmp_path, monkeypatch):
    sim, _ = setup(tmp_path, monkeypatch)
    import pytest
    with pytest.raises(SystemExit):
        account.cmd_buy(sim, "ABC", 7.5, "test")
    account.cmd_buy(sim, "ABC", 7.5, "user said so", user_decided=True)
    assert {p["symbol"] for p in sim.state()["positions"]} == {"XYZ", "ABC"}
