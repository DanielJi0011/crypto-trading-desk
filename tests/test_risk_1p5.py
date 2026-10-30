"""2026-10-30 用户决定：单笔风险 1.5%，保持 1 倍杠杆；max_open_risk_pct 联动到 0.04。离线。"""
import pytest

from desk import demo_exec as DX, okx_demo as X, settings, sizing, add_position as AP, config_safety

INSTR = {"ctVal": "0.01", "lotSz": "0.01", "minSz": "0.01", "tickSz": "0.1"}
E = 100000


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    from desk import equity_guard
    monkeypatch.setattr(equity_guard, "MARKS", tmp_path / "m.json")
    monkeypatch.setattr(X, "EVENTS", tmp_path / "ev.jsonl")
    monkeypatch.setattr(X, "_inst_cache", {k: INSTR for k in ("BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP")})
    monkeypatch.setattr(DX, "today_realized", lambda: 0.0); monkeypatch.setattr(DX, "today_orders", lambda: 0)
    return settings.load_config()


def test_config_values_linked(cfg):
    r, sw, g = cfg["risk"], cfg["swap"], cfg["guardrails"]
    assert r["risk_per_trade_pct"] == 0.015 and r["max_open_risk_pct"] == 0.04
    assert r["max_open_risk_pct"] / r["risk_per_trade_pct"] == pytest.approx(0.02 / 0.0075)   # 同比例
    # 不动项
    # 2026-10-30 用户决定：单笔保证金 20%→33%、总名义 180%→100%（见 tests/test_margin33_tm_add.py）；单笔名义不动
    assert r["max_margin_pct"] == 0.33 and r["max_notional_per_trade_pct"] == 1.0
    assert g["max_total_notional_pct"] == 100 and g["max_position_pct"] == 33
    assert sw["leverage_max"] == 1 and g["leverage"] == 1 and g["allow_leverage"] is False
    assert config_safety.max_open_leverage(cfg) == 1 if hasattr(config_safety, "max_open_leverage") else True


def test_size_1p5_unclipped_wide_stop(cfg):
    rc = sizing.rcfg(cfg)
    # 止损 9.8%+0.2% 缓冲=10% → 名义 15000 = 15%，保证金 15% < 20%，不被卡
    r = sizing.size(rc, equity=E, S=1, entry=100, stop=90.2, side="long", full_size=1, half_below=0, lev_cap=1, mmr=0.005, atr=1)
    assert r["ok"] and r["risk_usd"] == pytest.approx(1500) and r["notional"] == pytest.approx(15000)
    assert r["leverage"] == 1 and not r["clipped"]


def test_size_1p5_margin_clip_at_1x(cfg):
    rc = {**sizing.rcfg(cfg), "max_margin_pct": 0.20}   # 显式旧值 20%（新值 33% 见 test_margin33_tm_add.py）
    # 止损 3.8%+0.2%=4% → 原始名义 37500；1 倍杠杆无法提杠杆 → 削到 20000（20%）
    r = sizing.size(rc, equity=E, S=1, entry=100, stop=96.2, side="long", full_size=1, half_below=0, lev_cap=1, mmr=0.005, atr=1)
    assert r["ok"] and r["leverage"] == 1 and r["notional"] == pytest.approx(20000)
    assert [c["limit"] for c in r["clipped"]] == ["max_margin_pct"]
    assert r["stop_loss_usd"] == pytest.approx(20000 * 0.038)   # 实际风险 760 < 1500


def test_clip_to_risk_contracts_1p5(cfg):
    bk = {"equity": E}
    # BTC 100000，止损 5% → 每张(0.01)亏 50；上限 1500 → 30 张
    assert float(DX.clip_to_risk(cfg, bk, "BTC-USDT-SWAP", "40", 100000, 95000)) == pytest.approx(30)
    assert DX.clip_to_risk(cfg, bk, "BTC-USDT-SWAP", "30", 100000, 95000) == "30"


def test_check_open_risk_per_trade_1p5(cfg):
    cfg["guardrails"]["max_position_pct"] = 20   # 显式旧值 20%（新值 33% 见 test_margin33_tm_add.py）
    bk = {"equity": E, "live_equity": E, "positions": [], "triggers": [], "exposure": 0, "pending_entries": []}
    r, ch = DX.check_open(cfg, bk, "BTC-USDT-SWAP", 0.31, 100000, 95000, "long", 1)
    assert any("[RISK_PER_TRADE]" in x for x in r) and ch["risk_cap_usd"] == pytest.approx(1500)
    r, ch = DX.check_open(cfg, bk, "BTC-USDT-SWAP", 0.2, 100000, 95000, "long", 1)   # 亏损 1000，保证金 20%
    assert not any("[RISK_PER_TRADE]" in x or "[POSITION]" in x for x in r)
    r, _ = DX.check_open(cfg, bk, "BTC-USDT-SWAP", 0.21, 100000, 99000, "long", 1)   # 保证金 21% > 20%
    assert any("[POSITION]" in x for x in r)


def _pos(inst, side, notional, px, sl):
    return {"inst_id": inst, "side": side, "notional": notional, "price": px, "avg_px": px, "sl_px": sl}


def test_open_risk_4pct_allows_two_full_rejects_third(cfg):
    # 两笔各 1500 风险在册（3000），第三笔再加 1500 → 4500 > 4000 拒绝；加 900 → 3900 通过
    pos = [_pos("ETH-USDT-SWAP", "long", 15000, 2000, 1800), _pos("SOL-USDT-SWAP", "short", 15000, 100, 110)]
    bk = {"equity": E, "live_equity": E, "positions": pos, "triggers": [], "pending_entries": [], "exposure": 30000}
    r, ch = DX.check_open(cfg, bk, "BTC-USDT-SWAP", 0.15, 100000, 90000, "long", 1)
    assert any("[OPEN_RISK]" in x for x in r) and ch["open_risk_after"] == pytest.approx(4500)
    r, ch = DX.check_open(cfg, bk, "BTC-USDT-SWAP", 0.09, 100000, 90000, "long", 1)
    assert not any("[OPEN_RISK]" in x for x in r) and ch["open_risk_after"] == pytest.approx(3900)


def test_headroom_uses_4pct(cfg):
    rc = sizing.rcfg(cfg)
    room, lim = sizing.headroom(rc, E, {"net": 0, "open_risk": 3000}, "long", 0.05)
    assert lim == "max_open_risk_pct" and room == pytest.approx(1000 / 0.05)


def test_add_budget_cap_follows_risk_pct(cfg):
    rc = sizing.rcfg(cfg)
    pos = {"side": "long", "avg_px": 100, "sl_px": 95, "qty": 200, "leverage": 1, "margin": 5000}   # 现有风险 1000
    out = AP.budget(rc, pos, 100, 95, {"risk_usd": 1500, "notional": 20000}, E, 20, 10)
    assert out["cap"] == pytest.approx(1500) and out["existing_risk"] == pytest.approx(1000)
    assert out["ok"] and out["notional"] == pytest.approx(500 / 0.052)   # 剩余 500 / (5%+0.2%)
    pos2 = {**pos, "qty": 320}   # 现有风险 1600 > 1500 → add_budget_full
    out2 = AP.budget(rc, pos2, 100, 95, {"risk_usd": 1500, "notional": 20000}, E, 20, 10)
    assert not out2["ok"] and out2["reason"] == "add_budget_full"
