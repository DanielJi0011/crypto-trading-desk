"""第二批 R1–R4 验收（任务书 R1 表格；leverage_max 在用例中作为测试参数单独设定）。"""
import pytest
from desk import sizing

RC = {**sizing.RISK_DEFAULTS, "cost_buffer_pct": 0.0}
E = 100000.0


def _size(S, stop_pct, lev_cap=5, tm_lev=None, rc=RC, side="long", atr=0.5, **kw):
    entry = 100.0
    stop = entry * (1 - stop_pct) if side == "long" else entry * (1 + stop_pct)
    return sizing.size(rc, equity=E, S=S, entry=entry, stop=stop, side=side, full_size=0.6, half_below=0.3,
                       lev_cap=lev_cap, mmr=0.005, atr=atr, tm_lev_cap=tm_lev, **kw)


@pytest.mark.parametrize("S,stop,tm,notional,lev,margin", [
    (0.6, 0.05, None, 15000, 1, 15000),
    (0.6, 0.02, None, 37500, 2, 18750),
    (0.6, 0.01, None, 75000, 4, 18750),
    (0.6, 0.01, 2, 40000, 2, 20000),
    (0.45, 0.03, None, 18750, 1, 18750),
    (0.2, 0.05, None, 2500, 1, 2500),
])
def test_r1_table(S, stop, tm, notional, lev, margin):
    r = _size(S, stop, tm_lev=tm)
    assert r["ok"]
    assert r["notional"] == pytest.approx(notional, rel=1e-6)
    assert r["leverage"] == lev
    assert r["margin"] == pytest.approx(margin, rel=1e-6)
    assert r["stop_loss_usd"] <= E * 0.0075 + 1e-6


def test_r1_cost_buffer():
    r = _size(0.6, 0.05, rc={**RC, "cost_buffer_pct": 0.002})
    assert r["notional"] == pytest.approx(750 / 0.052, rel=1e-4)


def test_r1_lev_cap_1_clips_notional_by_margin():
    r = _size(0.6, 0.01, lev_cap=1)
    assert r["leverage"] == 1 and r["notional"] == pytest.approx(20000)
    assert any(c["limit"] == "max_margin_pct" for c in r["clipped"])


def test_r1_drawdown_scale():
    r = _size(0.6, 0.05, drawdown=0.05, max_drawdown=0.10)
    assert r["risk_usd"] == pytest.approx(375)


def test_r1_too_small():
    r = _size(0.2, 0.05, min_order_usd=1e9)
    assert not r["ok"] and r["reason"] == "size_too_small"


def test_r2_stop_atr_and_tp():
    sl, tp, d = sizing.stop_take("long", 100.0, 2.0, RC)
    assert d == pytest.approx(0.03) and sl == pytest.approx(97) and tp == pytest.approx(106)
    sl, tp, d = sizing.stop_take("short", 100.0, 20.0, RC)   # 30% 夹到 8%
    assert d == pytest.approx(0.08) and sl == pytest.approx(108) and tp == pytest.approx(84)
    _, _, d = sizing.stop_take("long", 100.0, 0.1, RC)
    assert d == pytest.approx(0.01)


def _st(a_conf, b_conf):
    return {"A": {"direction": "bullish", "confidence": a_conf, "entry_px": 110, "entry_condition": "gte", "horizon_days": 5},
            "B": {"direction": "bullish", "confidence": b_conf, "entry_px": 95, "entry_condition": "lte", "horizon_days": 5}}


def test_r3_liq_buffer_lowers_leverage():
    # 1% 止损需 4x；ATR=30 时 4x 强平距离不足 → 逐级降到满足为止
    r = _size(0.6, 0.01, atr=30.0)
    assert r["ok"] and r["leverage"] < 4
    assert abs(r["liq_px"] - 99.0) >= 30.0 - 1e-6


def test_r3_reject_at_1x():
    r = _size(0.6, 0.05, side="short", atr=200.0)
    assert not r["ok"] and r["reason"] == "liq_buffer_reject"


def test_r4_net_directional_headroom():
    bk = {"positions": [{"side": "short", "notional": 60000, "price": 100, "sl_px": 102},
                        {"side": "short", "notional": 50000, "price": 100, "sl_px": 102}]}
    ex = sizing.book_exposure(bk, sizing.RISK_DEFAULTS)
    room, lim = sizing.headroom({**sizing.RISK_DEFAULTS, "max_open_risk_pct": 1.0}, E, ex, "short", 0.05)
    assert room == pytest.approx(10000) and lim == "max_net_directional_pct"
    room_long, _ = sizing.headroom({**sizing.RISK_DEFAULTS, "max_open_risk_pct": 1.0}, E, ex, "long", 0.05)
    assert room_long == pytest.approx(230000)


def test_r4_open_risk_headroom():
    bk = {"positions": [{"side": "long", "notional": 50000, "price": 100, "sl_px": 97}]}   # 1500 风险
    ex = sizing.book_exposure(bk, sizing.RISK_DEFAULTS)
    room, lim = sizing.headroom(sizing.RISK_DEFAULTS, E, ex, "short", 0.05)
    assert lim == "max_open_risk_pct" and room == pytest.approx(500 / 0.05)
