"""2026-10-28 用户决定：交易经理 hold 且 new_stop/new_tp 留空 → 保持原止损止盈；给了值按其执行；减仓/缺席不变。"""
import pytest
from types import SimpleNamespace as NS

from desk import manager, settings


def _setup(monkeypatch):
    moves = {"stop": [], "tp": []}
    monkeypatch.setattr("desk.demo_exec.move_stop", lambda cfg, p, sl: moves["stop"].append(sl) or (True, f"stop→{sl}"))
    monkeypatch.setattr("desk.demo_exec.move_tp", lambda cfg, p, tp: moves["tp"].append(tp) or (True, f"tp→{tp}"))
    pos = {"inst_id": "BTC-USDT-SWAP", "side": "long", "venue": "okx_demo", "contracts": 2,
           "avg_px": 100.0, "sl_px": 90.0, "tp_px": 120.0, "price": 105.0}
    st = {r: {"direction": "bullish", "confidence": 1.0, "stop": 96.0, "target": 112.0} for r in manager.VOTERS}
    w = {r: 1.0 for r in manager.VOTERS}
    return moves, pos, st, w, {"last": 105.0, "atr14": 2.0}


def test_hold_empty_fields_keeps_sltp(monkeypatch):
    moves, pos, st, w, mk = _setup(monkeypatch)
    notes = []
    manager.apply_position_sl_tp(settings.load_config(), pos, st, w, mk,
                                 NS(action="hold", new_stop=None, new_tp=None), "hold", None, notes)
    assert moves == {"stop": [], "tp": []}
    assert notes == ["交易经理持有不动，保持原止损止盈"]


def test_hold_variant_label_also_keeps(monkeypatch):
    moves, pos, st, w, mk = _setup(monkeypatch)
    notes = []
    manager.apply_position_sl_tp(settings.load_config(), pos, st, w, mk,
                                 NS(action="hold", new_stop=None, new_tp=None), "hold(价格漂移)", None, notes)
    assert moves == {"stop": [], "tp": []} and manager.HOLD_KEEP_NOTE in notes


def test_hold_with_tm_levels_uses_them(monkeypatch):
    moves, pos, st, w, mk = _setup(monkeypatch)
    notes = []
    manager.apply_position_sl_tp(settings.load_config(), pos, st, w, mk,
                                 NS(action="hold", new_stop=95.0, new_tp=115.0), "hold", None, notes)
    assert moves["stop"] == [pytest.approx(95.0)] and moves["tp"] == [pytest.approx(115.0)]
    assert manager.HOLD_KEEP_NOTE not in notes


def test_hold_with_only_new_stop_still_weighted_tp(monkeypatch):
    moves, pos, st, w, mk = _setup(monkeypatch)
    notes = []
    manager.apply_position_sl_tp(settings.load_config(), pos, st, w, mk,
                                 NS(action="hold", new_stop=95.0, new_tp=None), "hold", None, notes)
    assert moves["stop"] == [pytest.approx(95.0)] and moves["tp"] == [pytest.approx(112.0)]


def test_reduce_unchanged_uses_weighted(monkeypatch):
    moves, pos, st, w, mk = _setup(monkeypatch)
    notes = []
    manager.apply_position_sl_tp(settings.load_config(), pos, st, w, mk,
                                 NS(action="reduce", new_stop=None, new_tp=None), "reduce",
                                 {"ok": True, "remaining": 1}, notes)
    assert moves["stop"] == [pytest.approx(96.0)] and moves["tp"] == [pytest.approx(112.0)]
    assert manager.HOLD_KEEP_NOTE not in notes


def test_open_and_no_position_untouched(monkeypatch):
    moves, pos, st, w, mk = _setup(monkeypatch)
    notes = []
    for act in ("buy", "short", "pending", "sell"):
        manager.apply_position_sl_tp(settings.load_config(), pos, st, w, mk,
                                     NS(action=act, new_stop=None, new_tp=None), act, None, notes)
    manager.apply_position_sl_tp(settings.load_config(), None, st, w, mk,
                                 NS(action="hold", new_stop=None, new_tp=None), "hold", None, notes)
    assert moves == {"stop": [], "tp": []} and notes == []


def test_tm_absent_keeps_old_weighted_behavior(monkeypatch):
    moves, pos, st, w, mk = _setup(monkeypatch)
    notes = []
    manager.apply_position_sl_tp(settings.load_config(), pos, st, w, mk,
                                 NS(action="hold", new_stop=None, new_tp=None), "hold(交易经理缺席)", None, notes,
                                 tm_absent=True)
    assert moves["stop"] == [pytest.approx(96.0)] and manager.HOLD_KEEP_NOTE not in notes


def test_switch_off_and_default_when_missing(monkeypatch):
    moves, pos, st, w, mk = _setup(monkeypatch)
    cfg = settings.load_config()
    assert cfg["manager"]["tm_hold_keep_sltp"] is True
    dec = NS(action="hold", new_stop=None, new_tp=None)
    cfg["manager"].pop("tm_hold_keep_sltp")
    assert manager.hold_keep_sltp(cfg, dec, "hold") is True        # 旧配置缺字段 → 默认新行为
    cfg["manager"]["tm_hold_keep_sltp"] = False
    notes = []
    manager.apply_position_sl_tp(cfg, pos, st, w, mk, dec, "hold", None, notes)
    assert moves["stop"] == [pytest.approx(96.0)] and moves["tp"] == [pytest.approx(112.0)]
