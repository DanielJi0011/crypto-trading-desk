"""杠杆上限（Q11，2026-10-22 用户决定）：新开仓按 1 倍；guardrails.leverage / allow_leverage 被代码执行。
5 倍场景只作为测试参数单独设定，验证夹取与降杠杆逻辑仍正确。离线，不发请求。"""
from types import SimpleNamespace


from desk import config_safety, demo_exec as DX, ledger, manager, settings


def _lev(cfg, lev):
    """下单工具定张数时的杠杆夹取（manager._tool_contracts，margin_pct 路径）。"""
    tm = SimpleNamespace(leverage=lev, risk_scale=1.0, _order_tool={"margin_pct": 10})
    bk = {"equity": 100000.0, "live_equity": 100000.0}
    agg = {"stop_loss": 105.0, "side": "short", "entry_px": 100.0}
    from desk import okx_demo as X
    X._inst_cache.setdefault("BTC-USDT-SWAP", {"ctVal": "0.01", "lotSz": "0.01", "minSz": "0.01", "tickSz": "0.1"})
    return manager._tool_contracts(cfg, "BTC-USDT-SWAP", bk, tm, agg, {"last": 100.0}, 100.0, 105.0)[1]


def test_default_new_open_leverage_is_1():
    cfg = settings.load_config()
    assert config_safety.max_open_leverage(cfg) == 1.0
    assert _lev(cfg, 5) == 1.0      # 交易经理给 5 倍 → 夹到 1
    assert _lev(cfg, 1) == 1.0


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


def test_param_5x_used_6_clamped():
    cfg = _cfg5()
    assert config_safety.max_open_leverage(cfg) == 5
    assert _lev(cfg, 5) == 5.0
    assert _lev(cfg, 6) == 5.0
    assert ledger.liq_price("long", 100.0, 5, ledger.swap_cfg(cfg)["maint_margin_rate"]) < 100.0


def test_allow_leverage_false_caps_even_if_swap_higher():
    cfg = settings.load_config()
    cfg["swap"]["leverage_max"] = 5
    assert config_safety.max_open_leverage(cfg) == 1.0
