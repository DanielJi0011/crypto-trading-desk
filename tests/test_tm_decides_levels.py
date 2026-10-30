"""2026-10-30 用户决定：交易经理定加仓腿点位、现有挂单的保留/重挂/有效期；代码只做硬护栏。离线。"""
import datetime as _d

import pytest

from desk import add_position as AP, demo_exec as DX, manager, settings, sizing
from tests.test_pending_entry_fix import _run, EXT


def _tm(**k):
    base = dict(action="pending", confidence=0.6, rationale="", entry_side="short", entry_trigger_px=79300,
                entry_condition="gte", entry_stop_loss=81950, entry_take_profit=73200, entry_size_pct=5,
                entry_expire_hours=96, risk_scale=0.5)
    base.update(k)
    return manager.ManagerDecision.model_construct(**{**{f: None for f in manager.ManagerDecision.model_fields}, **base})


@pytest.fixture
def cfg():
    return settings.load_config()


# ---------- 加仓腿点位 ----------
def test_btc_add_leg_uses_tm_levels(cfg):
    notes = []
    lv = manager.tm_add_levels(cfg, _tm(), "short", 79300, 1500.0, notes)
    assert lv == (81950, 73200) and any("以交易经理为准" in n for n in notes)
    p = AP.plan_sl_tp(sizing.rcfg(cfg), "short", 79300, 1500.0, *lv, src="交易经理")
    assert p["stop"] == pytest.approx(81950) and p["take_profit"] == pytest.approx(73200)


def test_add_leg_tm_stop_wrong_side_or_too_far_dropped(cfg):
    for bad in (78000, 79300 * 1.2):
        notes = []
        lv = manager.tm_add_levels(cfg, _tm(entry_stop_loss=bad), "short", 79300, 1500.0, notes)
        assert lv is None or lv[0] is None
        assert any("弃用" in n for n in notes)


def test_add_leg_rr_guard_extends_tp(cfg):
    notes = []
    lv = manager.tm_add_levels(cfg, _tm(entry_take_profit=78500), "short", 79300, 1500.0, notes)
    p = AP.plan_sl_tp(sizing.rcfg(cfg), "short", 79300, 1500.0, *lv, src="交易经理")
    assert p["rr"] >= 1.5 - 1e-9 and p["take_profit"] < 78500


def test_add_leg_fallback_when_off_or_absent(cfg):
    assert manager.tm_add_levels(cfg, None, "short", 79300, 1500.0, []) is None
    cfg["manager"]["tm_decides_add_levels"] = False
    assert manager.tm_add_levels(cfg, _tm(), "short", 79300, 1500.0, []) is None
    cfg["manager"]["tm_decides_add_levels"] = True
    assert manager.tm_add_levels(cfg, _tm(action="hold", entry_side=None, entry_trigger_px=None, entry_stop_loss=None,
                                          entry_take_profit=None), "short", 79300, 1500.0, []) is None


# ---------- 现有挂单动作判定 ----------
def test_existing_action_rules():
    n = []
    assert manager.tm_existing_pending_action(_tm(rationale="本次重挂旧单"), n) == "replace" and n
    n = []
    assert manager.tm_existing_pending_action(_tm(entry_reason="保留现有挂单"), n) == "keep"
    n = []
    assert manager.tm_existing_pending_action(_tm(existing_pending="keep", rationale="撤旧挂新"), n) == "keep"
    assert any("矛盾" in x for x in n)
    n = []
    assert manager.tm_existing_pending_action(_tm(rationale="重挂", entry_reason="保留原单"), n) == "keep"
    assert any("歧义" in x for x in n)
    assert manager.tm_existing_pending_action(_tm(), []) == "keep"


ETX = dict(EXT, trigger_px=2334.0)


def _eth(**k):
    return _tm(**{**dict(entry_side="short", entry_trigger_px=2334, entry_stop_loss=2432, entry_take_profit=2160,
                         entry_expire_hours=144), **k})


def test_sol_style_replace_goes_place_trigger(monkeypatch):
    r, c = _run(monkeypatch, dict(ETX), _eth(rationale="本次重挂旧单，张数按风险定"))
    assert len(c["trig"]) == 1 and c["amend"] == [] and c["trig"][0]["trigger_px"] == 2334


def test_keep_switch_off_old_behavior(monkeypatch):
    orig = settings.load_config
    def off():
        c = orig(); c["manager"]["tm_decides_existing_pending"] = False; return c
    monkeypatch.setattr(settings, "load_config", off)
    r, c = _run(monkeypatch, dict(ETX), _eth(rationale="本次重挂旧单"))
    assert c["trig"] == []


# ---------- 有效期 ----------
def _reg(monkeypatch, tmp_path, created):
    import json
    p = tmp_path / "reg.json"
    p.write_text(json.dumps({"positions": {}, "triggers": {"OLD": {"created": created, "expires": "2026-11-04 06:09:42+0800"}}}))
    monkeypatch.setattr(DX, "REG", p)
    from desk import okx_demo
    monkeypatch.setattr(okx_demo, "log_event", lambda *a, **k: None)


def test_eth_expiry_from_created(monkeypatch, tmp_path, cfg):
    _reg(monkeypatch, tmp_path, "2026-10-30 06:09:43+0800")
    monkeypatch.setattr(settings, "now", lambda: _d.datetime(2026, 10, 30, 21, 0, tzinfo=_d.timezone(_d.timedelta(hours=8))))
    n = []
    r = manager.apply_tm_expiry(cfg, "ETH-USDT-SWAP", "OLD", _eth(entry_expire_hours=118), n)
    assert r["new"] == "2026-11-04 04:09:43+0800"
    assert DX.reg()["triggers"]["OLD"]["expires"] == "2026-11-04 04:09:43+0800"


def test_expiry_already_passed_kept(monkeypatch, tmp_path, cfg):
    _reg(monkeypatch, tmp_path, "2026-10-20 06:09:43+0800")
    monkeypatch.setattr(settings, "now", lambda: _d.datetime(2026, 10, 30, 21, 0, tzinfo=_d.timezone(_d.timedelta(hours=8))))
    n = []
    assert manager.apply_tm_expiry(cfg, "ETH-USDT-SWAP", "OLD", _eth(entry_expire_hours=118), n) is None
    assert DX.reg()["triggers"]["OLD"]["expires"] == "2026-11-04 06:09:42+0800" and any("已过" in x for x in n)


def test_existing_pending_field_backward_compatible():
    d = manager.ManagerDecision.model_fields["existing_pending"]
    assert d.default is None


def test_eth_keep_path_sets_expiry(monkeypatch, tmp_path):
    _reg(monkeypatch, tmp_path, "2026-10-30 06:09:43+0800")
    seen = []
    monkeypatch.setattr(settings, "now", lambda: _d.datetime(2026, 10, 30, 21, 0, tzinfo=_d.timezone(_d.timedelta(hours=8))))
    monkeypatch.setattr(DX, "set_trigger_expiry", lambda inst, a, e, why: seen.append((a, e)) or True)
    r, c = _run(monkeypatch, dict(ETX), _eth(entry_reason="保留现有挂单", entry_expire_hours=118))
    assert c["trig"] == [] and seen and seen[0][1] == "2026-11-04 04:09:43+0800"
