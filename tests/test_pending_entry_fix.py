"""2026-10-30 入场方案修复：改动1（没写价只算方向票）、改动2（对现有挂单字段）、改动3（交易经理条件单推广 + 同价位保留改点 + 漂移豁免）。"""
import pytest
from desk import settings, manager, sizing, stances
from desk import demo_exec as DX

W = {"a": 1.0, "b": 1.0, "c": 1.0, "tm": 1.5}


def S(conf=0.6, **k):
    return {"direction": "bearish", "confidence": conf, **k}


# ---------- 改动 1 ----------
def test_only_tm_and_trader_wrote_price():
    st = {"a": S(), "b": S(), "c": S(entry_px=103, entry_condition="gte"), "tm": S(entry_px=103, entry_condition="gte")}
    p = sizing.choose_plan(st, W, list(st), "short", 100, 0.003, no_entry_not_now=True)
    assert p["group"] == "pullback" and p["entry"] == pytest.approx(103) and sorted(p["direction_only"]) == ["a", "b"]


def test_old_behavior_now_wins_when_switch_off():
    st = {"a": S(0.8), "b": S(0.8), "c": S(0.8), "tm": S(0.5, entry_px=103, entry_condition="gte")}
    assert sizing.choose_plan(st, W, list(st), "short", 100, 0.003)["group"] == "now"
    assert sizing.choose_plan(st, W, list(st), "short", 100, 0.003, no_entry_not_now=True)["group"] == "pullback"


def test_nobody_wrote_price_falls_back_now():
    st = {r: S() for r in W}
    p = sizing.choose_plan(st, W, list(st), "short", 100, 0.003, no_entry_not_now=True)
    assert p["group"] == "now" and p["no_entry_fallback_now"] and p["direction_only"] == []


def test_explicit_now_still_counts():
    st = {"a": S(0.9, entry_condition="now"), "b": S(0.9, entry_condition="now"), "c": S(),
          "tm": S(0.3, entry_px=103, entry_condition="gte")}
    p = sizing.choose_plan(st, W, list(st), "short", 100, 0.003, no_entry_not_now=True)
    assert p["group"] == "now" and p["direction_only"] == ["c"]


def test_conflict_threshold():
    # now 组 1×0.5=0.5 vs 回踩组 1×0.55=0.55：差距 <20% → 冲突
    st = {"a": S(0.5, entry_condition="now"), "b": S(0.55, entry_px=103, entry_condition="gte"), "c": S()}
    p = sizing.choose_plan(st, W, list(st), "short", 100, 0.003, no_entry_not_now=True)
    assert p["conflict"]
    st["b"]["confidence"] = 0.9
    assert not sizing.choose_plan(st, W, list(st), "short", 100, 0.003, no_entry_not_now=True)["conflict"]


# ---------- 改动 2 ----------
EX = {"trigger_px": 103.0, "condition": "gte", "side": "short"}


def test_parse_pending_action():
    for word, v in (("保留", "keep"), ("改价", "reprice"), ("撤销", "cancel"), ("改现价", "now")):
        s = stances.parse_stance(f"x\n【立场】方向=看空；目标价=90；止损价=110；置信度=0.6；周期=3天；对现有挂单={word}")
        assert s and s.get("pending_action") == v, word
    s = stances.parse_stance("x\n【立场】方向=看空；目标价=90；止损价=110；置信度=0.6；周期=3天")
    assert s.get("pending_action") is None


def test_default_keep_and_actions():
    st = {"a": S(), "b": S(pending_action="keep"), "c": S(pending_action="cancel"), "tm": S(pending_action="now")}
    p = sizing.choose_plan(st, W, list(st), "short", 100, 0.003, no_entry_not_now=True, existing=EX)
    assert sorted(p["implied_keep"]) == ["a", "b"] and p["cancel_votes"] == ["c"]
    assert p["members"]["now"] == ["tm"] and p["entry"] == pytest.approx(103)
    assert p["group"] == "pullback"   # a+b=1.2 > tm 0.9


def test_reprice_uses_own_price():
    st = {"a": S(0.9, entry_px=105, entry_condition="gte", pending_action="reprice"), "b": S(0.3)}
    p = sizing.choose_plan(st, W, list(st), "short", 100, 0.003, no_entry_not_now=True, existing=EX)
    assert p["entry"] == pytest.approx((105 * 0.9 + 103 * 0.3) / 1.2)


def test_aggregate_switch_off_ignores_existing(monkeypatch):
    cfg = settings.load_config(); cfg.setdefault("sizing", {})["pending_action_field"] = False
    st = {"trader": {"direction": "bearish", "confidence": 0.7}}
    a = manager.aggregate(cfg, st, {"trader": 1.0}, {"last": 100.0, "atr14": 2.0}, existing=dict(EX))
    assert not a.get("plan_implied_keep")


# ---------- 改动 3：纯函数 ----------
def test_keep_sltp_rules():
    f = sizing.pending_keep_sltp
    r = f("short", 2334, 2432, 2185, 2432, 2160, 2290)          # ETH 实例：只改止盈
    assert r["changed"] and r["take_profit"] == 2160 and r["stop_loss"] == 2432 and r["rr"] >= 1.5
    r = f("short", 79900, 81950, 76400, 82300, 76000, 77200)    # 止损放宽 → 保持；止盈可改
    assert r["stop_loss"] == 81950 and r["take_profit"] == 76000 and any("放宽" in x for x in r["reasons"])
    r = f("short", 100, 104, 95, None, 99.95, 100)              # 止盈距现价不足 0.3% 且 RR 不够
    assert not r["changed"]
    r = f("short", 100, 104, 95, 103, 101, 98)                  # 止盈不在盈利侧
    assert r["take_profit"] == 95 and r["stop_loss"] == 103
    r = f("short", 100, 104, 95, 101, 99, 98)                   # RR<1.5 → 都保持
    assert not r["changed"] and any("RR" in x for x in r["reasons"])


# ---------- 改动 3：执行路径 ----------
def _tm(**k):
    base = dict(action="pending", confidence=0.6, rationale="等反弹", entry_side="short", entry_trigger_px=2334,
                entry_condition="gte", entry_stop_loss=2432, entry_take_profit=2160, entry_size_pct=5, entry_expire_hours=48)
    base.update(k)
    return manager.ManagerDecision.model_construct(**{**{f: None for f in manager.ManagerDecision.model_fields}, **base})


def _run(monkeypatch, ex, tm, drift_now=None, src="daily"):
    calls = {"open": [], "trig": [], "amend": []}
    agg = {"buy": True, "side": "short", "condition": "now", "entry_px": 2290.0, "stop_loss": 2400.0, "take_profit": 2100.0,
           "horizon_days": 3, "S": -0.4, "notes": [], "leverage": 1.0}
    monkeypatch.setattr(manager, "aggregate", lambda *a, **k: dict(agg))
    monkeypatch.setattr(manager, "pre_trade_gate", lambda *a, **k: None)
    monkeypatch.setattr(manager, "_cooldown", lambda *a, **k: False)
    monkeypatch.setattr(manager, "demo_size", lambda *a: {"ok": True, "leverage": 1.0, "margin": 1, "risk_usd": 1,
                                                         "notional": 1, "stop_loss_usd": 1})
    monkeypatch.setattr(DX, "existing_trigger", lambda bk, inst: ex)
    monkeypatch.setattr(DX, "cancel_triggers", lambda *a, **k: [])
    monkeypatch.setattr(DX, "open_now", lambda *a, **k: (calls["open"].append(a), (True, {}))[1])
    monkeypatch.setattr(DX, "place_trigger", lambda cfg, bk, inst, d: (calls["trig"].append(d), (True, {"algoId": "N", "sz": 1}))[1])
    monkeypatch.setattr(DX, "amend_trigger_sltp", lambda cfg, inst, e, sl, tp: (calls["amend"].append((sl, tp)),
                                                                                {"ok": True, "how": "amend", "algoId": e["algoId"]})[1])
    from desk import drift, okx_demo
    monkeypatch.setattr(okx_demo, "log_event", lambda *a, **k: None)

    def fake_skip(cfg, inst, did, mk, a, notes, kind):
        if drift_now is None:
            return False
        a["drift_skipped"] = {"now_px": drift_now}; return True
    monkeypatch.setattr(drift, "skip", fake_skip)
    cfg = settings.load_config()
    bk = {"equity": 1e4, "live_equity": 1e4, "positions": [], "triggers": []}
    r = manager._pm_execute_demo_locked(cfg, cfg["coins"][0], "D", {}, {}, {"last": 2290.0}, bk, None, "t",
                                        source=src, tm=tm, tm_state="go")
    return r, calls


EXT = {"algoId": "OLD", "side": "short", "trigger_px": 2334.0, "condition": "gte", "stop_loss": 2432.0,
       "take_profit": 2185.0, "sz": "3", "expires": ""}


def test_same_px_keeps_and_amends_tp(monkeypatch):
    r, c = _run(monkeypatch, dict(EXT), _tm(), drift_now=2264.0)   # 漂移 -1.1% 也不跳过
    assert r["action"] == "pending" and c["trig"] == [] and c["open"] == [] and c["amend"] == [(None, 2160.0)]


def test_new_tm_trigger_drift_exempt(monkeypatch):
    r, c = _run(monkeypatch, None, _tm(entry_trigger_px=2350, entry_take_profit=2150), drift_now=2264.0)
    assert c["open"] == [] and len(c["trig"]) == 1 and c["trig"][0]["trigger_px"] == 2350


def test_crossed_trigger_still_skipped(monkeypatch):
    r, c = _run(monkeypatch, None, _tm(entry_trigger_px=2250, entry_stop_loss=2350, entry_take_profit=2100), drift_now=2264.0)
    assert c["trig"] == [] and r["action"] == "hold(价格漂移)"


def test_now_order_still_drift_skipped(monkeypatch):
    r, c = _run(monkeypatch, None, _tm(action="short", entry_side=None), drift_now=2264.0)
    assert c["open"] == [] and r["action"] == "hold(价格漂移)"


def test_post_stop_uses_tm_trigger(monkeypatch):
    r, c = _run(monkeypatch, None, _tm(entry_trigger_px=2350, entry_take_profit=2150), src="post_stop:x")
    assert c["open"] == [] and len(c["trig"]) == 1


def test_switch_off_old_behavior(monkeypatch):
    orig = settings.load_config
    def off():
        c = orig(); c["manager"]["tm_pending_override"] = False; c["manager"]["tm_pending_drift_exempt"] = False; return c
    monkeypatch.setattr(settings, "load_config", off)
    r, c = _run(monkeypatch, None, _tm())
    assert len(c["open"]) == 1 and c["trig"] == []


def test_fake_okx_amend_attach(monkeypatch):
    from tests.fake_okx import FakeOKX
    f = FakeOKX().install(monkeypatch)
    f.algos.append({"algoId": "OLD", "attachAlgoOrds": [{"slTriggerPx": "2432", "tpTriggerPx": "2185"}]})
    monkeypatch.setattr(DX, "reg", lambda: {"positions": {}, "triggers": {}})
    monkeypatch.setattr(DX, "save_reg", lambda r: None)
    from desk import okx_demo
    monkeypatch.setattr(okx_demo, "log_event", lambda *a, **k: None)
    res = DX.amend_trigger_sltp(settings.load_config(), "ETH-USDT-SWAP", dict(EXT), None, 2160.0)
    assert res["how"] == "amend" and f.algos[0]["attachAlgoOrds"][0]["tpTriggerPx"] == "2160.00"
