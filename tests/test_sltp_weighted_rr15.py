"""开仓/早晚场调点：ATR+2R 基准 → 全员 w×置信度加权 → RR≥min_rr_after_adjust；TM new_* 同一护栏；position_brief 含 upnl。"""
import pytest
from types import SimpleNamespace as NS

from desk import sizing, manager, analysis, settings


RC = {**sizing.RISK_DEFAULTS}

# feat/sltp-anchor-fix：本文件断言旧的止损止盈加权行为（止损 96 > 1.25×基准、止盈不封顶），显式关闭新规则开关
@pytest.fixture(autouse=True)
def _old_sltp_rules(monkeypatch):
    orig = settings.load_config

    def _load():
        cfg = orig()
        cfg.setdefault("sizing", {}).update(sltp_anchor_entry=False, sltp_rr_tighten_stop_first=False,
                                            sltp_tp_cap=False, sltp_exclude_self_levels=False)
        return cfg
    monkeypatch.setattr(settings, "load_config", _load)



def test_rr_ratio_definition():
    assert sizing.rr_ratio("long", 100, 97, 106) == pytest.approx(2.0)
    assert sizing.rr_ratio("short", 100, 103, 94) == pytest.approx(2.0)
    assert sizing.rr_ratio("long", 100, 103, 106) is None   # 止损在错侧
    assert sizing.rr_ratio("long", 100, 97, None) is None


def test_wavg_levels_confidence_weight():
    st = {
        "a": {"direction": "bullish", "confidence": 1.0, "stop": 90.0, "target": 120.0},
        "b": {"direction": "bullish", "confidence": 0.5, "stop": 95.0, "target": 110.0},
        "c": {"direction": "bullish", "confidence": 1.0, "stop": 105.0, "target": 80.0},  # 错侧剔除
    }
    w = {"a": 1.0, "b": 1.0, "c": 1.0}
    lv = sizing.wavg_levels(st, w, ["a", "b", "c"], "long", 100.0)
    # stop: (1*90 + 0.5*95) / 1.5 = 91.666...
    assert lv["stop"] == pytest.approx((90 + 47.5) / 1.5)
    assert lv["target"] == pytest.approx((120 + 55) / 1.5)
    assert lv["stop_n"] == 2 and lv["target_n"] == 2


def test_finalize_expands_tp_to_min_rr():
    # 基准 RR=2；角色把止盈拉近到 RR=1.0 → 外推到 1.5
    fin = sizing.finalize_sl_tp("long", 100, 97, 106, role_stop=97, role_tp=100.5, min_rr=1.5)
    assert fin["stop"] == pytest.approx(97)
    assert fin["take_profit"] == pytest.approx(100 + 1.5 * 3)
    assert fin["rr"] == pytest.approx(1.5)
    assert any("外推" in n for n in fin["notes"])


def test_finalize_keeps_baseline_when_no_roles():
    fin = sizing.finalize_sl_tp("long", 100, 97, 106, None, None, min_rr=1.5)
    assert fin["stop"] == 97 and fin["take_profit"] == 106
    assert fin["adjusted"] is False and fin["rr"] == pytest.approx(2.0)


def test_guard_rr_rejects_below_min():
    ok, msg, rr = sizing.guard_rr("long", 100, 97, 100.5, 1.5)
    assert not ok and rr == pytest.approx(0.5 / 3) and "RR" in msg
    ok, _, rr = sizing.guard_rr("long", 100, 97, 106, 1.5)
    assert ok and rr == pytest.approx(2.0)
    # 无止盈不拦
    assert sizing.guard_rr("long", 100, 97, None, 1.5)[0]


def test_aggregate_atr_baseline_unchanged_without_role_levels():
    """无角色 stop/target 时行为与 R2 纯 ATR+2R 一致（回归 test_r2_aggregate）。"""
    cfg = settings.load_config()
    st = {r: {"direction": "neutral", "confidence": 0} for r in manager.VOTERS}
    r1 = manager.VOTERS[0]
    st[r1] = {"direction": "bullish", "confidence": 0.9, "entry_px": 100, "entry_condition": "now", "horizon_days": 5}
    w = {r: (1.0 if r == r1 else 0.01) for r in manager.VOTERS}
    agg = manager.aggregate(cfg, st, w, {"last": 100.0, "atr14": 2.0})
    assert agg["buy"] and agg["stop_loss"] == pytest.approx(97.0)
    assert agg["take_profit"] == pytest.approx(106.0)
    assert agg["rr"] == pytest.approx(2.0)
    assert agg["levels_adjusted"] is False


def test_aggregate_weighted_adjust_then_min_rr():
    cfg = settings.load_config()
    assert float(cfg["risk"]["min_rr_after_adjust"]) == 1.5
    st = {r: {"direction": "neutral", "confidence": 0} for r in manager.VOTERS}
    # 两名同向：把止损收到 98、止盈收到 101（相对入场 100，RR=0.5）→ 应外推止盈
    a, b = manager.VOTERS[0], manager.VOTERS[1]
    st[a] = {"direction": "bullish", "confidence": 1.0, "entry_px": 100, "entry_condition": "now",
             "horizon_days": 5, "stop": 98.0, "target": 101.0}
    st[b] = {"direction": "bullish", "confidence": 1.0, "entry_px": 100, "entry_condition": "now",
             "horizon_days": 5, "stop": 98.0, "target": 101.0}
    w = {r: (1.0 if r in (a, b) else 0.01) for r in manager.VOTERS}
    agg = manager.aggregate(cfg, st, w, {"last": 100.0, "atr14": 2.0})
    assert agg["buy"] and agg["levels_adjusted"]
    assert agg["stop_loss"] == pytest.approx(98.0)
    # 止损距=2，min_rr=1.5 → tp=103
    assert agg["take_profit"] == pytest.approx(103.0)
    assert agg["rr"] == pytest.approx(1.5)
    assert agg["baseline_stop"] == pytest.approx(97.0)


def test_apply_new_stop_rejects_low_rr(monkeypatch):
    seen = []
    monkeypatch.setattr("desk.demo_exec.move_stop", lambda *a, **k: seen.append(a) or (True, "ok"))
    cfg = settings.load_config()
    pos = {"inst_id": "BTC-USDT-SWAP", "side": "long", "venue": "okx_demo", "contracts": 1,
           "avg_px": 100.0, "sl_px": 95.0, "tp_px": 102.0, "price": 100.0}
    # new_stop=99 → 止损距1，tp=102 → RR=2 OK? wait tp dist=2, sl dist=1, RR=2
    # Use tp that makes RR low: keep tp=102, new_stop=90 → sl_dist=10, tp_dist=2, RR=0.2
    notes = []
    manager.apply_new_stop(cfg, pos, NS(new_stop=90.0, new_tp=None), "hold", None, notes)
    assert not seen and any("RR" in n for n in notes)


def test_apply_new_tp_rejects_low_rr(monkeypatch):
    seen = []
    monkeypatch.setattr("desk.demo_exec.move_tp", lambda *a, **k: seen.append(a) or (True, "ok"))
    cfg = settings.load_config()
    pos = {"inst_id": "BTC-USDT-SWAP", "side": "long", "venue": "okx_demo", "contracts": 1,
           "avg_px": 100.0, "sl_px": 97.0, "tp_px": 106.0, "price": 101.0}
    notes = []
    manager.apply_new_tp(cfg, pos, NS(new_tp=100.5, new_stop=None), "hold", None, notes)
    assert not seen and any("RR" in n for n in notes)


def test_apply_position_uses_weighted_when_no_tm(monkeypatch):
    moves = {"stop": [], "tp": []}
    monkeypatch.setattr("desk.demo_exec.move_stop", lambda cfg, p, sl: moves["stop"].append(sl) or (True, f"stop→{sl}"))
    monkeypatch.setattr("desk.demo_exec.move_tp", lambda cfg, p, tp: moves["tp"].append(tp) or (True, f"tp→{tp}"))
    cfg = settings.load_config()
    pos = {"inst_id": "BTC-USDT-SWAP", "side": "long", "venue": "okx_demo", "contracts": 1,
           "avg_px": 100.0, "sl_px": 90.0, "tp_px": 120.0, "price": 105.0}
    st = {r: {"direction": "bullish", "confidence": 1.0, "stop": 96.0, "target": 112.0} for r in manager.VOTERS}
    w = {r: 1.0 for r in manager.VOTERS}
    mk = {"last": 105.0, "atr14": 2.0}  # baseline stop=97, tp=106 at entry 100
    notes = []
    dec = NS(new_stop=None, new_tp=None)
    manager.apply_position_sl_tp(cfg, pos, st, w, mk, dec, "hold", None, notes)
    # 角色加权 stop=96 相对现止损 90 是收紧；tp 角色 112，RR=(112-100)/(100-96)=3≥1.5
    assert moves["stop"] == [pytest.approx(96.0)]
    assert moves["tp"] == [pytest.approx(112.0)]


def test_position_brief_includes_upnl():
    bk = {"positions": [
        {"inst_id": "ETH-USDT-SWAP", "side": "short", "pos_side": "short", "qty": 1.0, "contracts": 10,
         "avg_px": 2500.0, "sl_px": 2600.0, "tp_px": 2300.0, "upnl": 12.5},
    ]}
    b = analysis.position_brief(bk, "ETH-USDT-SWAP")
    assert "浮盈亏 +12.50 USDT" in b and "（本币）" in b


def test_config_safety_registers_min_rr():
    from desk import config_safety
    assert "risk.min_rr_after_adjust" in config_safety.ENFORCED
    cfg = settings.load_config()
    config_safety.validate(cfg)  # 不得因新键报错
