"""2026-10-30 用户决定：单笔保证金 33%（risk.max_margin_pct 0.33 与 guardrails.max_position_pct 33 联动）、
总名义 ≤100%（同向净名义 ≤100%）、是否加仓由交易经理决定（add_position.tm_decides_add，预算只作参考）。离线。"""
import copy

import pytest

from desk import add_position as AP, config_safety, demo_exec as DX, manager as MG, okx_demo as X, settings, sizing
from tests.test_add_position import base, _mgr, _short, _bk  # noqa: F401  复用夹具

E = 100000.0
INSTR = {"ctVal": "0.01", "lotSz": "0.01", "minSz": "0.01", "tickSz": "0.1"}


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    from desk import equity_guard
    monkeypatch.setattr(equity_guard, "MARKS", tmp_path / "m.json")
    monkeypatch.setattr(X, "EVENTS", tmp_path / "ev.jsonl")
    monkeypatch.setattr(X, "_inst_cache", {k: INSTR for k in ("BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP")})
    monkeypatch.setattr(DX, "today_realized", lambda: 0.0); monkeypatch.setattr(DX, "today_orders", lambda: 0)
    return settings.load_config()


def test_config_values(cfg):
    g, r, sw = cfg["guardrails"], cfg["risk"], cfg["swap"]
    assert g["max_position_pct"] == 33 and r["max_margin_pct"] == 0.33
    assert g["max_total_notional_pct"] == 100 and g["max_total_exposure_pct"] == 100
    assert r["max_net_directional_pct"] == 1.0 and r["max_notional_per_trade_pct"] == 1.0
    assert cfg["manager"]["max_size_pct"] == 33
    assert cfg["add_position"]["tm_decides_add"] is True and AP.tm_decides(cfg)
    assert cfg["manager"]["tm_required_for_open"] is True
    assert sw["leverage_max"] == 1 and g["leverage"] == 1 and g["allow_leverage"] is False
    assert config_safety.max_open_leverage(cfg) == 1
    config_safety.validate(cfg)


def test_config_safety_linkage(cfg):
    c = copy.deepcopy(cfg); c["risk"]["max_margin_pct"] = 0.20
    with pytest.raises(config_safety.ConfigSafetyError, match="max_margin_pct"):
        config_safety.validate(c)
    c = copy.deepcopy(cfg); c["risk"]["max_net_directional_pct"] = 1.2
    with pytest.raises(config_safety.ConfigSafetyError, match="max_net_directional_pct"):
        config_safety.validate(c)


def test_size_margin33_clip_1x(cfg):
    rc = sizing.rcfg(cfg)
    # 止损 3.8%+0.2% → 原始名义 37500；1 倍杠杆 → 削到 33000（33%）
    r = sizing.size(rc, equity=E, S=1, entry=100, stop=96.2, side="long", full_size=1, half_below=0, lev_cap=1, mmr=0.005, atr=1)
    assert r["ok"] and r["leverage"] == 1 and r["notional"] == pytest.approx(33000)
    assert [c["limit"] for c in r["clipped"]] == ["max_margin_pct"]
    assert r["stop_loss_usd"] == pytest.approx(33000 * 0.038)
    # 1 倍强平价远在止损外，不误拒（宽止损 8%）
    r = sizing.size(rc, equity=E, S=1, entry=100, stop=92, side="long", full_size=1, half_below=0, lev_cap=1, mmr=0.005, atr=5)
    assert r["ok"] and r["liq_px"] < 92 - 5


def _bk0(positions=(), eq=E):
    return {"equity": eq, "live_equity": eq, "positions": list(positions), "triggers": [], "pending_entries": [],
            "exposure": sum(p["notional"] for p in positions), "cash": eq}


def _p(inst, side, notional, px, sl):
    return {"inst_id": inst, "side": side, "notional": notional, "price": px, "avg_px": px, "sl_px": sl,
            "qty": notional / px, "margin": notional, "leverage": 1.0}


def test_check_open_margin33(cfg):
    r, _ = DX.check_open(cfg, _bk0(), "BTC-USDT-SWAP", 0.33, 100000, 99000, "long", 1)
    assert not any("[POSITION]" in x for x in r), r
    r, _ = DX.check_open(cfg, _bk0(), "BTC-USDT-SWAP", 0.34, 100000, 99000, "long", 1)
    assert any("[POSITION]" in x for x in r)


def test_check_open_total_notional_100(cfg):
    held = [_p("ETH-USDT-SWAP", "long", 33000, 2000, 1980), _p("SOL-USDT-SWAP", "short", 40000, 100, 101)]
    r, _ = DX.check_open(cfg, _bk0(held), "BTC-USDT-SWAP", 0.26, 100000, 99000, "long", 1)   # 73000+26000=99000
    assert not any("[EXPOSURE]" in x for x in r), r
    r, ch = DX.check_open(cfg, _bk0(held), "BTC-USDT-SWAP", 0.28, 100000, 99000, "long", 1)  # 101000 > 100000
    assert any("[EXPOSURE]" in x for x in r)


def test_check_open_net_directional_100(cfg):
    held = [_p("ETH-USDT-SWAP", "long", 33000, 2000, 1980), _p("SOL-USDT-SWAP", "long", 33000, 100, 99)]
    c = copy.deepcopy(cfg); c["guardrails"]["max_total_notional_pct"] = 200   # 隔离：只看同向净名义
    r, _ = DX.check_open(c, _bk0(held), "BTC-USDT-SWAP", 0.33, 100000, 99000, "long", 1)   # 99000 ≤ 100000
    assert not any("[NET_DIRECTIONAL]" in x for x in r), r
    r, _ = DX.check_open(c, _bk0(held), "BTC-USDT-SWAP", 0.35, 100000, 99000, "long", 1)   # 101000
    assert any("[NET_DIRECTIONAL]" in x for x in r)


# ---------------------------------------------------------------- 加仓：预算只作参考
def _pos_big():
    # 原仓风险 = (82600-80760.4)×0.3 ≈ 551.9，远超 risk_usd 100 → 旧行为 add_budget_full
    return _short(qty=0.3, lev=1.0)


def test_budget_tm_decides_not_rejected(cfg):
    rc = sizing.rcfg(cfg); p = _pos_big()
    sz = {"ok": True, "risk_usd": 100.0, "notional": 5000.0, "clipped": []}
    old = AP.budget(rc, p, 80000, 82000, sz, E, 33, 10)
    assert not old["ok"] and old["reason"] == "add_budget_full"
    new = AP.budget(rc, p, 80000, 82000, sz, E, 33, 10, tm_decides=True, total_room=E - p["notional"], total_now=p["notional"], max_total_pct=100)
    assert new["ok"] and new["notional"] == pytest.approx(5000) and new["over_budget"] is True
    assert "add_risk_budget" not in new["clip_by"]
    assert new["margin_after_pct"] == pytest.approx((p["margin"] + 5000) / E * 100, rel=1e-3)


def test_budget_tm_decides_hard_guards(cfg):
    rc = sizing.rcfg(cfg)
    p = _short(qty=0.3, lev=1.0)   # 保证金 24000
    sz = {"ok": True, "risk_usd": 1500.0, "notional": 20000.0, "clipped": [], "room": 50000.0, "room_limit": "max_open_risk_pct"}
    a = AP.budget(rc, p, 80000, 82000, sz, E, 33, 10, tm_decides=True, total_room=50000.0)
    assert a["ok"] and a["notional"] == pytest.approx(9000) and "max_position_pct(合并保证金)" in a["clip_by"]
    assert a["hard_max_notional"] == pytest.approx(9000) and a["hard_limit"].startswith("max_position_pct")
    a = AP.budget(rc, p, 80000, 82000, sz, E, 33, 10, tm_decides=True, total_room=5000.0)
    assert a["ok"] and a["notional"] == pytest.approx(5000) and "max_total_notional_pct" in a["clip_by"]
    a = AP.budget(rc, p, 80000, 82000, sz, E, 33, 10, tm_decides=True, total_room=0.0)
    assert not a["ok"] and a["reason"] == "add_total_notional_full"
    p2 = _short(qty=0.42, lev=1.0)   # 保证金 33600 > 33%
    a = AP.budget(rc, p2, 80000, 82000, sz, E, 33, 10, tm_decides=True, total_room=50000.0)
    assert not a["ok"] and a["reason"] == "add_margin_full"
    a = AP.budget(rc, p, 80000, None, sz, E, 33, 10, tm_decides=True, total_room=50000.0)
    assert not a["ok"] and a["reason"] == "no_stop"


@pytest.fixture
def mbase(base):
    # _mgr 的 aggregate 桩是旧签名（不收 tm）：与 test_add_position 相同，关闭入场方案一开关以隔离；加仓开关用默认值
    base.setdefault("manager", {}).update(tm_no_vote_weight=False, tm_decides_entry=False)
    return base


def test_manager_tm_decides_add_not_budget_rejected(mbase, monkeypatch):
    base = mbase
    res, c = _mgr(monkeypatch, base, sz_info={"ok": True, "risk_usd": 100.0, "notional": 10000.0, "clipped": [],
                                               "exposure": {"long": 0, "short": 4832.0}})
    assert res["action"] == "add_short" and len(c["add_now"]) == 1
    a = res["aggregate"]["add"]
    assert a["ok"] and a["tm_decides"] and a["over_budget"] and "add_risk_budget" not in a["clip_by"]
    assert any("仅参考" in n for n in res["notes"])


def test_manager_switch_off_restores_budget(mbase, monkeypatch):
    base = mbase
    base.setdefault("add_position", {})["tm_decides_add"] = False
    res, c = _mgr(monkeypatch, base, sz_info={"ok": True, "risk_usd": 100.0, "notional": 10000.0, "clipped": []})
    assert not c["add_now"] and res["aggregate"]["add"]["reason"] == "add_budget_full"


def test_manager_total_notional_blocks_add(mbase, monkeypatch):
    base = mbase
    res, c = _mgr(monkeypatch, base, sz_info={"ok": True, "risk_usd": 750.0, "notional": 10000.0, "clipped": [],
                                               "exposure": {"long": 60000.0, "short": 40000.0}})
    assert not c["add_now"] and res["aggregate"]["add"]["reason"] == "add_total_notional_full"


@pytest.mark.parametrize("tm_state", ["absent", "veto"])
def test_manager_tm_absent_or_veto_no_add(mbase, monkeypatch, tm_state):
    base = mbase
    res, c = _mgr(monkeypatch, base, tm_state=tm_state)
    assert not c["add_now"] and not c["trig"] and res["action"].startswith("hold")


def test_preview_shows_reference_and_hard_max(monkeypatch, tmp_path):
    from desk import equity_guard as EG
    from tests.test_tm_add_preview import INST, INSTR as I2, _pos, _bk as _bk2, _st, MK
    monkeypatch.setattr(X, "_inst_cache", {INST: I2})
    monkeypatch.setattr(X, "EVENTS", tmp_path / "ev.jsonl")
    monkeypatch.setattr(EG, "MARKS", tmp_path / "m.json")
    monkeypatch.setattr(DX, "REG", tmp_path / "reg.json")
    monkeypatch.setattr(MG, "demo_size", lambda *a: {"ok": True, "risk_usd": 750.0, "notional": 20000.0, "clipped": [],
                                                     "exposure": {"long": 0.0, "short": 2380.0}})
    cfg = settings.load_config()
    p = _pos(qty=10.0)   # 原仓风险 950 > 预算 750，保证金 7933 未满
    pv = MG.add_preview(cfg, INST, MK, _bk2(p), p, _st(), {"trader": 1.0}, -0.5)
    it = pv["items"][0]
    assert it["within_budget"] is False and it["reason"] is None and it["sz"]
    s = MG.describe_add_preview(pv)
    assert "仅参考不否决" in s and "按硬护栏最多还能加" in s and "加仓后本币保证金占权益" in s and "总名义占权益" in s
