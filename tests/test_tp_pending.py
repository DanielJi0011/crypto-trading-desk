"""止盈后再讨论：交易经理用下单工具挂单/否决追单时不现价开仓，改挂条件单或观望；止损再讨论与早晚场同样以工具为准；场次标签。"""
from desk import settings, manager
from desk import demo_exec as DX


def _tm(**k):
    from tests._tool_tm import tool_tm
    base = dict(action="pending", rationale="不追空，等反弹", side="short", trigger_px=106.1,
                condition="gte", stop_loss=109.8, take_profit=99.0, expire_hours=120)
    base.update(k)
    return tool_tm(base.pop("action"), **base)


def _run(monkeypatch, source, tm, tm_state="go"):
    calls = {"open": [], "trig": []}
    agg = {"buy": True, "side": "short", "condition": "now", "entry_px": 100.4, "stop_loss": 107.4, "take_profit": 86.5,
           "horizon_days": 3, "S": -0.42, "notes": [], "leverage": 1.0}
    monkeypatch.setattr(manager, "aggregate", lambda *a, **k: dict(agg))
    monkeypatch.setattr(manager, "pre_trade_gate", lambda *a, **k: None)
    monkeypatch.setattr(manager, "_cooldown", lambda *a, **k: False)
    monkeypatch.setattr(manager, "demo_size", lambda cfg, a, *r: (calls.setdefault("sized", dict(a)), {
        "ok": True, "leverage": 1.0, "margin": 100.0, "risk_usd": 1, "notional": 100, "stop_loss_usd": 1})[1])
    from desk import okx_demo as X
    monkeypatch.setattr(X, "contracts", lambda inst, q: "1")
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
    assert any("action=pending" in n for n in r["notes"])


def test_tp_tm_veto_no_levels_holds(monkeypatch):
    r, c = _run(monkeypatch, "post_tp:x", _tm(action="hold", side=None, trigger_px=None, condition=None, stop_loss=None, take_profit=None, expire_hours=None), tm_state="veto")
    assert c["open"] == [] and c["trig"] == [] and r["action"].startswith("hold")


def test_tp_tm_pending_opposite_side_holds(monkeypatch):
    r, c = _run(monkeypatch, "post_tp:x", _tm(side="long", condition="lte", trigger_px=95, stop_loss=92))
    assert c["open"] == [] and c["trig"] == []


def test_sl_and_sessions_follow_tm_pending_when_on(monkeypatch):
    for src in ("post_stop:x", "daily", "evening"):
        r, c = _run(monkeypatch, src, _tm())
        assert c["open"] == [] and len(c["trig"]) == 1 and r["action"] == "pending", src
        assert c["trig"][0]["trigger_px"] == 106.1


def test_tp_tm_market_vote_still_market(monkeypatch):
    r, c = _run(monkeypatch, "post_tp:x", _tm(action="open", side="short", entry=100.4, trigger_px=None, condition=None, stop_loss=107.4, take_profit=86.5, expire_hours=None))
    assert len(c["open"]) == 1


def test_record_session(monkeypatch):
    monkeypatch.setattr(settings, "SESSION", "morning")
    assert manager.record_session("post_tp:e") == "rediscuss"
    assert manager.record_session("post_stop:e") == "rediscuss"
    assert manager.record_session("daily") == "morning"
    monkeypatch.setattr(settings, "SESSION", "evening")
    assert manager.record_session("daily") == "evening"
