"""2026-10-30 用户决定：持有期到期对该币跑一次与早晚报相同的全员讨论（expiry_extend.mode=full_discussion，默认）。"""
import datetime as dt
import json
import time

import pytest

from desk import settings, okx_demo as X, demo_exec as DX, expiry as EXP, expiry_discuss as ED

INST = "BTC-USDT-SWAP"


def _ts(**kw):
    return (settings.now() + dt.timedelta(**kw)).strftime(EXP.FMT)


@pytest.fixture
def world(monkeypatch, tmp_path):
    from desk import demo_watch, entry_limit, demo_ws, monitor, trigger_review
    monkeypatch.setattr(ED, "DIR", tmp_path / "expiry_discuss")
    past = _ts(hours=-1)
    store = {"reg": {"triggers": {}, "positions": {INST: {"deadline": past, "opened": _ts(days=-5), "horizon_days": 5}},
                     "watch": {"last_algo_uts": 1}}}
    w = {"close": [], "sent": [], "spawn": [], "store": store, "tmp": tmp_path, "deadline": past}
    monkeypatch.setattr(X, "verify", lambda: True)
    monkeypatch.setattr(X, "EVENTS", tmp_path / "ev.jsonl")
    monkeypatch.setattr(trigger_review, "poll", lambda *a, **k: {})
    monkeypatch.setattr(entry_limit, "check_timeouts", lambda cfg: [])
    monkeypatch.setattr(DX, "reg", lambda: json.loads(json.dumps(store["reg"])))
    monkeypatch.setattr(DX, "save_reg", lambda r: store.__setitem__("reg", json.loads(json.dumps(r))))
    monkeypatch.setattr(DX, "trigger_orders", lambda inst=None: [])
    monkeypatch.setattr(DX, "demo_positions", lambda: [])
    pos = {"inst_id": INST, "trade_id": "DEMO-BTC", "side": "long", "avg_px": 100.0, "price": 104.0,
           "upnl": 4.0, "sl_px": 95.0, "tp_px": 115.0}
    w["pos"] = pos
    monkeypatch.setattr(DX, "book", lambda cfg, prices=None: {"positions": [pos] if INST in store["reg"]["positions"] else [],
                                                              "equity": 1, "live_equity": 1, "triggers": []})

    def _close(cfg, p, why):
        w["close"].append(why); store["reg"]["positions"].pop(p["inst_id"], None)
    monkeypatch.setattr(DX, "close", _close)
    monkeypatch.setattr(demo_ws, "sync_snapshot", lambda *a, **k: [])
    monkeypatch.setattr(demo_ws, "notify", lambda ev: w["sent"].append(ev))
    monkeypatch.setattr(X, "get", lambda path, **kw: {"data": []})
    monkeypatch.setattr(monitor, "schedule_rediscuss", lambda *a, **k: None)
    monkeypatch.setattr(ED, "SPAWN", lambda args, logfile: w["spawn"].append(args))
    monkeypatch.setattr(EXP, "_call_tm", lambda *a: (_ for _ in ()).throw(AssertionError("full_discussion 不应调用简化问答")))
    cfg = settings.load_config()
    w["cfg"], w["run"] = cfg, (lambda: demo_watch.run_once(w["cfg"]))
    return w


def _events(w, name=None):
    p = w["tmp"] / "ev.jsonl"
    ev = [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []
    return [e for e in ev if name is None or e["event"] == name]


def _m(w):
    return w["store"]["reg"]["positions"].get(INST)


def test_default_mode_is_full_discussion():
    assert EXP.mode(settings.load_config()) == "full_discussion"
    assert EXP.mode({"expiry_extend": {"enabled": True, "mode": "simple"}}) == "simple"
    assert EXP.mode({"expiry_extend": {"enabled": True, "mode": "off"}}) == "off"
    assert EXP.mode({"expiry_extend": {"enabled": False}}) == "off"


def test_expiry_triggers_full_discussion_once(world):
    world["run"]()
    m = _m(world)
    assert world["spawn"] == [["--coin", "BTC", "--deadline", world["deadline"]]]
    assert m["expiry_discussed_for"] == world["deadline"] and m["expiry_discussion"]["status"] == "started"
    assert world["close"] == [] and [e["kind"] for e in world["sent"]] == ["expiry_discussion_started"]
    # 讨论进行中：再跑两轮不重复启动、不重复通知、不平仓（保护单不动）
    world["run"](); world["run"]()
    assert len(world["spawn"]) == 1 and len(world["sent"]) == 1 and world["close"] == []
    assert len(_events(world, "expiry_discussion_started")) == 1


def test_restart_does_not_retrigger(world):
    world["run"]()
    from desk import demo_watch
    import importlib
    importlib.reload(demo_watch)   # 模拟重启：标记来自已落盘登记
    demo_watch.run_once(world["cfg"])
    assert len(world["spawn"]) == 1


def test_result_hold_extends_deadline(world):
    world["run"]()
    ED.write_result_once(INST, world["deadline"], {"status": "done", "outcome": "held", "final_action": "hold",
                                                   "consensus": 0.31, "decision_id": "D-X-BTC", "horizon_days": 3,
                                                   "reason": "全员讨论结论持有（hold，S=+0.310）"})
    world["run"]()
    m = _m(world)
    exp_new = (EXP._dt.datetime.strptime(world["deadline"], EXP.FMT) + dt.timedelta(days=3)).strftime(EXP.FMT)
    assert m["deadline"] == exp_new and m["extensions"] == 1 and m["orig_deadline"] == world["deadline"]
    assert world["close"] == []
    assert [e["kind"] for e in world["sent"]] == ["expiry_discussion_started", "position_extended"]
    world["run"]()   # 不重复应用
    assert _m(world)["extensions"] == 1 and len(world["sent"]) == 2
    ev = _events(world, "expiry_extended")
    assert len(ev) == 1 and ev[0]["mode"] == "full_discussion" and ev[0]["consensus"] == 0.31


def test_report_line_has_conclusion_and_S(world, monkeypatch):
    world["run"]()
    ED.write_result_once(INST, world["deadline"], {"status": "done", "outcome": "held", "final_action": "hold",
                                                   "consensus": 0.31, "decision_id": "D-X-BTC", "horizon_days": 5, "reason": "r"})
    world["run"]()
    L = EXP.report_lines(settings.today())
    assert any("全员讨论结论持有" in x and "S=+0.310" in x for x in L)


def test_default_horizon_5(world):
    world["run"]()
    ED.write_result_once(INST, world["deadline"], {"status": "done", "outcome": "adjusted", "final_action": "reduce", "consensus": 0.2})
    world["run"]()
    nd = EXP._dt.datetime.strptime(_m(world)["deadline"], EXP.FMT)
    assert nd - EXP._dt.datetime.strptime(world["deadline"], EXP.FMT) == dt.timedelta(days=5)


def test_discussion_failed_falls_back_close(world):
    world["run"]()
    ED.write_result_once(INST, world["deadline"], {"status": "failed", "reason": "全员讨论失败（RuntimeError），回退到期平仓"})
    world["run"]()
    assert world["close"] == ["持有期到期"] and _m(world) is None
    assert [e["kind"] for e in world["sent"]] == ["expiry_discussion_started", "position_expired"]
    assert _events(world, "expiry_closed")[0]["discussion_failed"] is True


def test_discussion_timeout_falls_back_close(world):
    world["run"]()
    world["store"]["reg"]["positions"][INST]["expiry_discussion"]["started"] = _ts(minutes=-60)
    world["run"]()
    assert world["close"] == ["持有期到期"]
    assert ED.read_result(INST, world["deadline"])["status"] == "failed"


def test_result_closed_no_double_close(world):
    world["run"]()
    ED.write_result_once(INST, world["deadline"], {"status": "done", "outcome": "closed", "final_action": "sell", "consensus": -0.3})
    world["run"]()
    assert world["close"] == [] and _m(world)["expiry_discussion"]["applied"] == world["deadline"]


def test_halt_no_discussion_no_close(world, monkeypatch):
    monkeypatch.setattr(settings, "halt_active", lambda: True)
    world["run"]()
    assert world["spawn"] == [] and world["close"] == []
    assert len(_events(world, "halt_skip_close")) == 1


def test_max_extensions_close_directly(world):
    world["store"]["reg"]["positions"][INST]["extensions"] = 2
    world["run"]()
    assert world["spawn"] == [] and world["close"] == ["持有期到期"]
    assert "已达延期上限" in _events(world, "expiry_closed")[0]["reason"]


def test_mode_off_closes_without_discussion(world):
    world["cfg"]["expiry_extend"]["mode"] = "off"
    world["run"]()
    assert world["spawn"] == [] and world["close"] == ["持有期到期"]


def test_mode_simple_uses_tm(world, monkeypatch):
    world["cfg"]["expiry_extend"]["mode"] = "simple"
    called = []
    monkeypatch.setattr(EXP, "_call_tm", lambda cfg, p: called.append(p) or EXP.ExpiryDecision(action="close", rationale="x"))
    monkeypatch.setattr(EXP, "latest_decision", lambda inst: None)
    from desk import manager
    monkeypatch.setattr(manager, "market_ctx", lambda inst: {"last": 104.0})
    world["run"]()
    assert called and world["spawn"] == [] and world["close"] == ["持有期到期"]


def test_schedule_nonblocking_background(world, monkeypatch):
    """不阻塞主循环：后台用独立会话子进程（与止损后再讨论同一方式），本轮立即返回。"""
    import subprocess
    seen = {}

    class P:
        def __init__(self, argv, **kw):
            seen["argv"], seen["kw"] = argv, kw
    monkeypatch.setattr(subprocess, "Popen", P)
    import importlib
    real = importlib.reload(ED)
    monkeypatch.setattr(real, "DIR", world["tmp"] / "ed2")
    monkeypatch.setattr(subprocess, "Popen", P)
    t = time.time()
    r = {"positions": {INST: {"deadline": world["deadline"]}}}
    assert real.schedule(world["cfg"], r, INST, r["positions"][INST], lambda x: None) is True
    assert time.time() - t < 2
    assert seen["argv"][1:3] == ["-m", "desk.expiry_discuss"] and seen["kw"]["start_new_session"] is True
    assert real.schedule(world["cfg"], r, INST, r["positions"][INST], lambda x: None) is False   # 同一 deadline 不再启动


# ---------------- 讨论进程本体（复用 analyze → stances.extract → decide_and_execute）
@pytest.fixture
def pipe(world, monkeypatch):
    from desk import analysis, stances, manager, okx_data, drift, scoring
    calls = {"brief": None, "source": None, "scored": 0}
    monkeypatch.setattr(okx_data, "last_price", lambda inst: 104.0)
    monkeypatch.setattr(DX, "is_demo", lambda cfg: True)
    monkeypatch.setattr(drift, "stamp", lambda inst: {"px": 104.0, "time": settings.now_str()})

    def _an(cfg, coin, date, bk, tracker, extra_brief=""):
        calls["brief"] = extra_brief
        if calls.get("raise"):
            raise RuntimeError("llm down after retries")
        return {"final_state": {}, "signal": "Hold", "report_dir": "x"}
    monkeypatch.setattr(analysis, "analyze", _an)
    monkeypatch.setattr(stances, "extract", lambda fs, cfg, tracker: {})
    calls["rec"] = {"decision_id": "D-X-BTC", "final_action": "hold", "consensus": 0.25, "tm_status": "hold",
                    "manager": {"action": "hold"}, "aggregate": {"horizon_days": 4}, "notes": []}

    def _dec(cfg, coin, date, fs, st, tracker, source="daily"):
        calls["source"] = source
        return calls["rec"]
    monkeypatch.setattr(manager, "decide_and_execute", _dec)
    monkeypatch.setattr(scoring, "record_stances", lambda *a: calls.__setitem__("scored", calls["scored"] + 1))
    world["store"]["reg"]["positions"][INST]["extensions"] = 1
    coin = next(c for c in world["cfg"]["coins"] if c["inst_id"] == INST)
    calls["go"] = lambda: ED.run(world["cfg"], coin, world["deadline"], tracker=object())
    return calls


def test_run_hold_result_and_brief(pipe, world):
    out = pipe["go"]()
    assert out["status"] == "done" and out["outcome"] == "held" and out["horizon_days"] == 4
    assert pipe["source"] == f"expiry:{world['deadline']}" and pipe["scored"] == 0
    assert "已持有" in pipe["brief"] and "已延期 1 次" in pipe["brief"] and world["deadline"] in pipe["brief"]
    assert pipe["go"]() == out   # 结果只写一次


def test_run_sell_closed_notifies_once(pipe, world):
    pipe["rec"].update(final_action="sell", consensus=-0.3)
    out = pipe["go"]()
    assert out["outcome"] == "closed"
    assert [e["kind"] for e in world["sent"]] == ["position_expired"]
    pipe["go"]()
    assert len(world["sent"]) == 1


def test_run_failure_and_tm_absent_failed(pipe, world):
    pipe["raise"] = True
    assert pipe["go"]()["status"] == "failed"


def test_run_tm_absent_failed(pipe, world):
    pipe["rec"]["tm_status"] = "absent"
    assert pipe["go"]()["status"] == "failed"


def test_run_halt_failed_no_llm(pipe, world, monkeypatch):
    monkeypatch.setattr(settings, "halt_active", lambda: True)
    assert pipe["go"]()["status"] == "failed" and pipe["brief"] is None


def test_record_session_and_order_count(monkeypatch, tmp_path):
    from desk import manager
    assert manager.record_session("expiry:2026-10-30 08:00:00+0800") == "expiry"
    monkeypatch.setattr(X, "EVENTS", tmp_path / "ev.jsonl")
    t = settings.now_str()
    settings.append_jsonl(X.EVENTS, {"event": "open_ok", "time": t, "source": "expiry"})
    settings.append_jsonl(X.EVENTS, {"event": "open_ok", "time": t, "source": "strategy"})
    assert DX.today_orders() == 1
