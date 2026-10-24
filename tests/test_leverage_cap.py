"""杠杆上限（Q11，2026-10-22 用户决定）：新开仓按 1 倍；guardrails.leverage / allow_leverage 被代码执行。
5 倍场景只作为测试参数单独设定，验证夹取与降杠杆逻辑仍正确。离线，不发请求。"""
from types import SimpleNamespace

import pytest

from desk import config_safety, demo_exec as DX, ledger, manager, settings


def test_default_new_open_leverage_is_1():
    cfg = settings.load_config()
    assert config_safety.max_open_leverage(cfg) == 1.0
    agg = {"stop_loss": 105.0, "side": "short", "entry_px": 100.0}
    assert manager._levfix(cfg, agg, SimpleNamespace(leverage=5)) == 1.0      # 交易经理给 5 倍 → 夹到 1
    assert manager._levfix(cfg, agg, SimpleNamespace(leverage=1)) == 1.0


def test_check_open_rejects_2x(monkeypatch, tmp_path):
    from desk import equity_guard
    monkeypatch.setattr(equity_guard, "MARKS", tmp_path / "m.json")
    monkeypatch.setattr(DX, "today_realized", lambda: 0.0); monkeypatch.setattr(DX, "today_orders", lambda: 0)
    cfg = settings.load_config()
    bk = {"equity": 100000, "live_equity": 100000, "positions": [], "triggers": [], "exposure": 0, "pending_entries": []}
    r, _ = DX.check_open(cfg, bk, "BTC-USDT-SWAP", 0.01, 100000, 99000, "long", 2)
    assert any("[LEVERAGE]" in x for x in r)


def _cfg5():
    cfg = settings.load_config()
    cfg["swap"]["leverage_max"] = 5
    cfg["guardrails"].update(leverage=5, allow_leverage=True)
    return cfg


def test_param_5x_used_6_clamped_and_liq_reduction():
    cfg = _cfg5()
    assert config_safety.max_open_leverage(cfg) == 5
    agg = {"stop_loss": 105.0, "side": "short", "entry_px": 100.0}
    assert manager._levfix(cfg, agg, SimpleNamespace(leverage=5)) == 5.0
    assert manager._levfix(cfg, agg, SimpleNamespace(leverage=6)) == 5.0
    lev = manager._levfix(cfg, {"stop_loss": 75.0, "side": "long", "entry_px": 100.0}, SimpleNamespace(leverage=6))
    assert 1 <= lev < 5 and 75.0 > ledger.liq_price("long", 100.0, lev, ledger.swap_cfg(cfg)["maint_margin_rate"])


def test_allow_leverage_false_caps_even_if_swap_higher():
    cfg = settings.load_config()
    cfg["swap"]["leverage_max"] = 5
    assert config_safety.max_open_leverage(cfg) == 1.0
