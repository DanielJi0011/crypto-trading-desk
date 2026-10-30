"""2026-10-30 feat/sltp-anchor-fix：止损锚定实际入场价、RR 不足先收紧止损、止盈封顶、pending 保持原点位、排除自我强化目标；各开关 false 恢复旧行为。"""
import copy
from types import SimpleNamespace as NS

import pytest

from desk import manager, settings, sizing

ATR = 1987.61
LAST = 77211.7


def _cfg(**sz):
    cfg = copy.deepcopy(settings.load_config())
    cfg.setdefault("sizing", {}).update(sz)
    return cfg


def _fin(cfg, side, entry, role_stop, role_tp, atr=ATR):
    rc = sizing.rcfg(cfg)
    bs, bt, _ = sizing.stop_take(side, entry, atr, rc)
    return sizing.finalize_sl_tp(side, entry, bs, bt, role_stop, role_tp, rc["min_rr_after_adjust"],
                                 **sizing.anchor_opts(cfg, rc, atr, entry))


def test_defaults_on():
    sc = sizing.scfg(settings.load_config())
    assert sc["sltp_anchor_entry"] and sc["sltp_rr_tighten_stop_first"] and sc["sltp_tp_cap"] and sc["sltp_exclude_self_levels"]
    assert sc["sltp_anchor_max_mult"] == 1.25 and sc["sltp_tp_cap_mult"] == 1.2
    assert settings.load_config()["manager"]["tm_pending_keep_sltp"] is True


def test_market_entry_far_stop_tightened_to_cap():
    # 止损离入场很远，但止盈足够让 RR≥1.5：只收紧到 1.25×基准
    base = 1.5 * ATR
    f = _fin(_cfg(), "short", LAST, 82300.0, LAST - 2.3 * base)
    assert f["stop"] == pytest.approx(LAST + 1.25 * base)
    assert any("收紧至" in n for n in f["notes"])


def test_rr_short_tightens_stop_first_then_tp():
    base = 1.5 * ATR
    f = _fin(_cfg(), "short", LAST, 82300.0, 76200.0)
    assert f["stop"] == pytest.approx(LAST + base)          # 先收紧到基准距离
    assert f["take_profit"] == pytest.approx(LAST - 1.5 * base)
    assert f["rr"] == pytest.approx(1.5)
    assert any("先把止损收紧" in n for n in f["notes"])


def test_tp_capped():
    base = 1.5 * ATR
    f = _fin(_cfg(), "long", 100000.0, None, 100000.0 + 5 * base)
    assert f["take_profit"] == pytest.approx(100000.0 + 1.2 * 2.0 * base)


def test_rr_still_short_after_cap_recorded_not_silent():
    # 关闭锚定与先收紧止损，只开封顶：止损 3×基准，外推需 4.5×基准 > 封顶 2.4×基准 → 如实记录
    base = 1.5 * ATR
    cfg = _cfg(sltp_anchor_entry=False, sltp_rr_tighten_stop_first=False)
    f = _fin(cfg, "long", 100000.0, 100000.0 - 3 * base, 100000.0 + base)
    assert f["take_profit"] == pytest.approx(100000.0 + 2.4 * base)
    assert f["rr_short"] == pytest.approx(0.8) and any("如实记录" in n for n in f["notes"])


def test_switches_off_restore_old():
    cfg = _cfg(sltp_anchor_entry=False, sltp_rr_tighten_stop_first=False, sltp_tp_cap=False)
    f = _fin(cfg, "short", LAST, 82300.0, 76200.0)
    assert f["stop"] == 82300.0
    assert f["take_profit"] == pytest.approx(LAST - 1.5 * (82300.0 - LAST))   # 旧：只外推止盈
    old = sizing.finalize_sl_tp("short", LAST, None, None, 82300.0, 76200.0, 1.5)
    assert (f["stop"], f["take_profit"]) == (old["stop"], old["take_profit"])


def _tm(**kw):
    d = dict(action="short", stop_loss=82300.0, take_profit=76400.0, horizon_days=5, entry_side="short",
             entry_trigger_px=None, entry_condition=None, entry_stop_loss=None, entry_take_profit=None, entry_size_pct=10.0)
    d.update(kw)
    return NS(**d)


def _stances():
    st = {r: {"direction": "bearish", "confidence": 0.7, "stop": 82300.0, "target": 76400.0 if i % 2 else 76000.0,
              "entry_px": 79900.0} for i, r in enumerate(manager.VOTERS)}
    return st, {r: 1.0 for r in manager.VOTERS}


def test_replay_btc_0824_market_entry():
    """今日 08:24 BTC 早场离线重放：现价 77211.7 入场，ATR14 1987.61，角色/交易经理止损 82300，目标 76400/76000。"""
    st, w = _stances()
    mk = {"last": LAST, "atr14": ATR}
    agg = manager.aggregate(_cfg(), st, w, mk, tm=_tm())
    base = 1.5 * ATR
    assert agg["condition"] == "now"
    assert agg["stop_loss"] == pytest.approx(LAST + base)                 # 80192.115
    assert agg["take_profit"] == pytest.approx(LAST - 1.5 * base)          # 72739.53
    old = manager.aggregate(_cfg(sltp_anchor_entry=False, sltp_rr_tighten_stop_first=False, sltp_tp_cap=False),
                            st, w, mk, tm=_tm())
    assert old["stop_loss"] == 82300.0 and old["take_profit"] == pytest.approx(69579.25, abs=0.01)


def test_pending_entry_keeps_tm_levels_only_checks():
    st, w = _stances()
    mk = {"last": LAST, "atr14": ATR}
    tm = _tm(action="pending", entry_trigger_px=79900.0, entry_condition="gte", entry_stop_loss=82300.0, entry_take_profit=75000.0)
    agg = manager.aggregate(_cfg(), st, w, mk, tm=tm)
    assert agg["plan_group"] == "tm_pending" and agg["condition"] != "now"
    assert agg["stop_loss"] == 82300.0 and agg["take_profit"] == 75000.0


def _pos_setup(monkeypatch):
    moves = {"stop": [], "tp": []}
    monkeypatch.setattr("desk.demo_exec.move_stop", lambda cfg, p, sl: moves["stop"].append(sl) or (True, f"stop→{sl}"))
    monkeypatch.setattr("desk.demo_exec.move_tp", lambda cfg, p, tp: moves["tp"].append(tp) or (True, f"tp→{tp}"))
    pos = {"inst_id": "BTC-USDT-SWAP", "side": "short", "venue": "okx_demo", "contracts": 6.29,
           "avg_px": 77170.85, "sl_px": 82300.0, "tp_px": 69579.25, "price": 78000.0}
    st, w = _stances()
    return moves, pos, st, w, {"last": 78000.0, "atr14": ATR}


@pytest.mark.parametrize("action", ["pending_add", "hold"])
def test_pending_keeps_position_levels(monkeypatch, action):
    moves, pos, st, w, mk = _pos_setup(monkeypatch)
    notes = []
    manager.apply_position_sl_tp(_cfg(), pos, st, w, mk, NS(action="pending", new_stop=None, new_tp=None), action, None, notes)
    assert moves == {"stop": [], "tp": []}
    assert notes == [manager.PENDING_KEEP_NOTE] and "保持原点位" in notes[0]


def test_pending_keep_switch_off_old_behavior(monkeypatch):
    moves, pos, st, w, mk = _pos_setup(monkeypatch)
    cfg = _cfg(sltp_anchor_entry=False, sltp_rr_tighten_stop_first=False, sltp_tp_cap=False, sltp_exclude_self_levels=False)
    cfg["manager"]["tm_pending_keep_sltp"] = False
    notes = []
    manager.apply_position_sl_tp(cfg, pos, st, w, mk, NS(action="pending", new_stop=None, new_tp=None), "pending_add", None, notes)
    assert manager.PENDING_KEEP_NOTE not in notes
    assert moves["tp"] and moves["tp"][0] == pytest.approx(77170.85 - 1.5 * (82300.0 - 77170.85))   # 旧：重新加权+外推


def test_pending_with_new_levels_not_kept(monkeypatch):
    moves, pos, st, w, mk = _pos_setup(monkeypatch)
    notes = []
    manager.apply_position_sl_tp(_cfg(), pos, st, w, mk, NS(action="pending", new_stop=80000.0, new_tp=None), "pending_add", None, notes)
    assert manager.PENDING_KEEP_NOTE not in notes


def test_drop_self_levels_pure():
    st = {"flows": {"target": 69579.25, "stop": 82300.0}, "tech": {"target": 74000.0, "stop": 80000.0}}
    out, dropped = sizing.drop_self_levels(st, {"tp_px": 69580.0, "sl_px": 82300.0}, 0.05)
    assert out["flows"]["target"] is None and out["flows"]["stop"] is None
    assert out["tech"] == st["tech"] and len(dropped) == 2


def test_self_target_excluded_in_position_adjust(monkeypatch):
    moves, pos, st, w, mk = _pos_setup(monkeypatch)
    roles = list(st)
    for r in roles:   # 所有角色目标 = 现有止盈 → 全部排除，止盈回到基准而非原样回收
        st[r]["target"] = 69579.25
        st[r]["stop"] = 80500.0
    notes = []
    manager.apply_position_sl_tp(_cfg(), pos, st, w, mk, NS(action="hold", new_stop=None, new_tp=None), "hold", None, notes,
                                 tm_absent=True)
    assert any("防自我强化" in n for n in notes)
    assert moves["tp"] and abs(moves["tp"][0] - 69579.25) > 1   # 不再原样回收现有止盈
    st2, _ = sizing.drop_self_levels(st, pos)
    assert all(s["target"] is None for s in st2.values())


def test_self_target_switch_off(monkeypatch):
    moves, pos, st, w, mk = _pos_setup(monkeypatch)
    for r in st:
        st[r]["target"] = 69579.25
    notes = []
    cfg = _cfg(sltp_exclude_self_levels=False)
    manager.apply_position_sl_tp(cfg, pos, st, w, mk, NS(action="hold", new_stop=None, new_tp=None), "hold", None, notes,
                                 tm_absent=True)
    assert not any("防自我强化" in n for n in notes)


def test_position_adjust_far_stop_tightened(monkeypatch):
    """已有持仓（无交易经理新点位、非 keep 情形，如交易经理缺席）：加权止损 82300 被收紧到均价基准附近。"""
    moves, pos, st, w, mk = _pos_setup(monkeypatch)
    notes = []
    manager.apply_position_sl_tp(_cfg(), pos, st, w, mk, NS(action="hold", new_stop=None, new_tp=None), "hold", None, notes,
                                 tm_absent=True)
    assert moves["stop"] and moves["stop"][0] == pytest.approx(77170.85 + 1.5 * ATR, rel=1e-6)
