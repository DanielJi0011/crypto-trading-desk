"""止盈后再讨论：交易经理投挂单/否决追单时不现价开仓，改挂条件单或观望；止损再讨论与早晚场不变；场次标签。"""
from desk import settings, manager
from desk import demo_exec as DX


def _tm(**k):
    base = dict(action="pending", confidence=0.6, rationale="不追空，等反弹", entry_side="short", entry_trigger_px=106.1,
                entry_condition="gte", entry_stop_loss=109.8, entry_take_profit=99.0, entry_size_pct=5, entry_expire_hours=120)
    base.update(k)
    return manager.ManagerDecision.model_construct(**{**{f: None for f in manager.ManagerDecision.model_fields}, **base})


def _run(monkeypatch, source, tm, tm_state="go"):
    calls = {"open": [], "trig": []}
    agg = {"buy": True, "side": "short", "condition": "now", "entry_px": 100.4, "stop_loss": 107.4, "take_profit": 86.5,
           "horizon_days": 3, "S": -0.42, "notes": [], "leverage": 1.0}
    monkeypatch.setattr(manager, "aggregate", lambda *a, **k: dict(agg))
    monkeypatch.setattr(manager, "pre_trade_gate", lambda *a, **k: None)
    monkeypatch.setattr(manager, "_cooldown", lambda *a, **k: False)
    monkeypatch.setattr(manager, "demo_size", lambda cfg, a, *r: (calls.setdefault("sized", dict(a)), {
        "ok": True, "leverage": 1.0, "margin": 100.0, "risk_usd": 1, "notional": 100, "stop_loss_usd": 1})[1])
    monkeypatch.setattr(DX, "cancel_triggers", lambda *a, **k: [])
    monkeypatch.setattr(DX, "open_now", lambda *a, **k: (calls["open"].append(a), (True, {}))[1])
    monkeypatch.setattr(DX, "place_trigger", lambda cfg, bk, inst, d: (calls["trig"].append(d), (True, {"algoId": "A1", "sz": 1}))[1])
    cfg = settings.load_config()
    bk = {"equity": 1e4, "live_equity": 1e4, "positions": [], "triggers": []}
    r = manager._pm_execute_demo_locked(cfg, cfg["coins"][0], "D-T", {}, {}, {"last": 100.4}, bk, None, "t",
                                        source=source, tm=tm, tm_state=tm_state)
    return r, calls


def test_tp_tm_pending_places_trigger_not_market(monkeypatch):
    r, c = _run(monkeypatch, "post_tp:x", _tm())
    assert c["open"] == [] and len(c["trig"]) == 1 and r["action"] == "pending"
    d = c["trig"][0]
    assert (d["trigger_px"], d["condition"], d["stop_loss"], d["take_profit"], d["expire_hours"]) == (106.1, "gte", 109.8, 99.0, 120.0)
    assert c["sized"]["entry_px"] == 106.1 and c["sized"]["stop_loss"] == 109.8
    assert any("不现价开仓" in n for n in r["notes"])


def test_tp_tm_veto_no_levels_holds(monkeypatch):
    r, c = _run(monkeypatch, "post_tp:x", _tm(action="hold", entry_side=None, entry_trigger_px=None), tm_state="veto")
    assert c["open"] == [] and c["trig"] == [] and r["action"].startswith("hold")


def test_tp_tm_pending_incomplete_holds(monkeypatch):
    # 若 tm_state 仍为 go 但点位不全（如 action=pending 缺止损），也观望并记 notes
    r, c = _run(monkeypatch, "post_tp:x", _tm(entry_stop_loss=None))
    assert c["open"] == [] and c["trig"] == [] and r["action"] == "hold(止盈后不追单)"
    assert any("观望不下单" in n for n in r["notes"])


def test_tp_tm_pending_opposite_side_holds(monkeypatch):
    r, c = _run(monkeypatch, "post_tp:x", _tm(entry_side="long", entry_condition="lte", entry_trigger_px=95, entry_stop_loss=92))
    assert c["open"] == [] and c["trig"] == []


def test_sl_and_sessions_unchanged_when_switch_off(monkeypatch):
    # 2026-10-30 改动 3 关闭开关时保持旧行为：止损再讨论与早晚场照共识现价开仓
    orig = settings.load_config
    def off():
        c = orig(); c.setdefault("manager", {})["tm_pending_override"] = False; return c
    monkeypatch.setattr(settings, "load_config", off)
    for src in ("post_stop:x", "daily", "evening"):
        r, c = _run(monkeypatch, src, _tm())
        assert len(c["open"]) == 1 and c["trig"] == [] and r["action"] == "short", src


def test_sl_and_sessions_follow_tm_pending_when_on(monkeypatch):
    for src in ("post_stop:x", "daily", "evening"):
        r, c = _run(monkeypatch, src, _tm())
        assert c["open"] == [] and len(c["trig"]) == 1 and r["action"] == "pending", src
        assert c["trig"][0]["trigger_px"] == 106.1


def test_tp_tm_market_vote_still_market(monkeypatch):
    r, c = _run(monkeypatch, "post_tp:x", _tm(action="short", entry_side=None))
    assert len(c["open"]) == 1


def test_record_session(monkeypatch):
    monkeypatch.setattr(settings, "SESSION", "morning")
    assert manager.record_session("post_tp:e") == "rediscuss"
    assert manager.record_session("post_stop:e") == "rediscuss"
    assert manager.record_session("daily") == "morning"
    monkeypatch.setattr(settings, "SESSION", "evening")
    assert manager.record_session("daily") == "evening"
