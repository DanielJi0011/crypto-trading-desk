"""2026-10-28 用户决定：交易经理 hold 且 new_stop/new_tp 留空 → 保持原止损止盈；给了值按其执行；减仓/缺席不变。"""
import pytest
from types import SimpleNamespace as NS

from desk import manager, settings

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


def test_hold_with_only_new_stop_keeps_tp(monkeypatch):
    """2026-10-29：只给 new_stop → 止损按交易经理，止盈保持原值（不再套加权 112）。"""
    moves, pos, st, w, mk = _setup(monkeypatch)
    notes = []
    manager.apply_position_sl_tp(settings.load_config(), pos, st, w, mk,
                                 NS(action="hold", new_stop=95.0, new_tp=None), "hold", None, notes)
    assert moves["stop"] == [pytest.approx(95.0)] and moves["tp"] == []
    assert manager.HOLD_SINGLE_NOTE in notes


def test_hold_with_only_new_tp_keeps_stop(monkeypatch):
    moves, pos, st, w, mk = _setup(monkeypatch)
    notes = []
    manager.apply_position_sl_tp(settings.load_config(), pos, st, w, mk,
                                 NS(action="hold", new_stop=None, new_tp=118.0), "hold", None, notes)
    assert moves["stop"] == [] and moves["tp"] == [pytest.approx(118.0)]


def test_hold_single_value_violating_rr_keeps_original(monkeypatch):
    """只给 new_tp=101：RR=(101-100)/(100-90)=0.1<1.5 → 该项保持原值并记原因，另一项也不动。"""
    moves, pos, st, w, mk = _setup(monkeypatch)
    notes = []
    manager.apply_position_sl_tp(settings.load_config(), pos, st, w, mk,
                                 NS(action="hold", new_stop=None, new_tp=101.0), "hold", None, notes)
    assert moves == {"stop": [], "tp": []} and any("未采纳" in n for n in notes)


def test_hold_single_stop_widen_rejected(monkeypatch):
    """止损只能收紧：只给 new_stop=85（宽于 90）→ 不放宽，止盈不动。"""
    moves, pos, st, w, mk = _setup(monkeypatch)
    notes = []
    manager.apply_position_sl_tp(settings.load_config(), pos, st, w, mk,
                                 NS(action="hold", new_stop=85.0, new_tp=None), "hold", None, notes)
    assert moves == {"stop": [], "tp": []}


def test_hold_single_switch_off_old_weighted(monkeypatch):
    moves, pos, st, w, mk = _setup(monkeypatch)
    cfg = settings.load_config()
    assert cfg["manager"]["tm_hold_keep_single_sltp"] is True
    dec = NS(action="hold", new_stop=95.0, new_tp=None)
    cfg["manager"].pop("tm_hold_keep_single_sltp")
    assert manager.hold_keep_single(cfg, dec, "hold") is True       # 缺字段 → 默认新行为
    cfg["manager"]["tm_hold_keep_single_sltp"] = False
    notes = []
    manager.apply_position_sl_tp(cfg, pos, st, w, mk, dec, "hold", None, notes)
    assert moves["stop"] == [pytest.approx(95.0)] and moves["tp"] == [pytest.approx(112.0)]


def test_reduce_with_single_value_still_weighted(monkeypatch):
    moves, pos, st, w, mk = _setup(monkeypatch)
    notes = []
    manager.apply_position_sl_tp(settings.load_config(), pos, st, w, mk,
                                 NS(action="reduce", new_stop=95.0, new_tp=None), "reduce", {"ok": True, "remaining": 1}, notes)
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
