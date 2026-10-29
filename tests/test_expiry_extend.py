"""2026-10-29 用户决定：持有期到期由交易经理判断延期或平仓；达上限/失败回退平仓；HALT 不平仓；重启不重复判断。"""
import datetime as dt
import json

import pytest

from desk import settings, okx_demo as X, demo_exec as DX, expiry as EXP


def _ts(**kw):
    return (settings.now() + dt.timedelta(**kw)).strftime(EXP.FMT)


@pytest.fixture
def world(monkeypatch, tmp_path):
    from desk import demo_watch, entry_limit, demo_ws, monitor, trigger_review, manager
    past = _ts(hours=-1)
    store = {"reg": {"triggers": {}, "positions": {"BTC-USDT-SWAP": {"deadline": past, "opened": _ts(days=-5), "horizon_days": 5}},
                     "watch": {"last_algo_uts": 1}}}
    w = {"close": [], "sent": [], "tm": [], "redis": 0, "store": store, "tmp": tmp_path}
    monkeypatch.setattr(X, "verify", lambda: True)
    monkeypatch.setattr(X, "EVENTS", tmp_path / "ev.jsonl")
    monkeypatch.setattr(trigger_review, "poll", lambda *a, **k: {})
    monkeypatch.setattr(entry_limit, "check_timeouts", lambda cfg: [])
    monkeypatch.setattr(DX, "reg", lambda: json.loads(json.dumps(store["reg"])))
    monkeypatch.setattr(DX, "save_reg", lambda r: store.__setitem__("reg", json.loads(json.dumps(r))))
    monkeypatch.setattr(DX, "trigger_orders", lambda inst=None: [])
    monkeypatch.setattr(DX, "demo_positions", lambda: [])
    pos = {"inst_id": "BTC-USDT-SWAP", "trade_id": "DEMO-BTC", "side": "long", "avg_px": 100.0, "price": 104.0,
           "upnl": 4.0, "sl_px": 95.0, "tp_px": 115.0}
    monkeypatch.setattr(DX, "book", lambda cfg, prices=None: {"positions": [pos], "equity": 1, "live_equity": 1, "triggers": []})

    def _close(cfg, p, why):
        w["close"].append(why); store["reg"]["positions"].pop(p["inst_id"], None)
    monkeypatch.setattr(DX, "close", _close)
    monkeypatch.setattr(demo_ws, "sync_snapshot", lambda *a, **k: [])
    monkeypatch.setattr(demo_ws, "notify", lambda ev: w["sent"].append(ev))
    monkeypatch.setattr(X, "get", lambda path, **kw: {"data": []})
    monkeypatch.setattr(monitor, "schedule_rediscuss", lambda *a, **k: w.__setitem__("redis", w["redis"] + 1))
    monkeypatch.setattr(manager, "market_ctx", lambda inst: {"last": 104.0})
    monkeypatch.setattr(EXP, "latest_decision", lambda inst: {"decision_id": "D-1", "consensus": w.get("S", 0.4), "stances": {}})
    monkeypatch.setattr("desk.llm_retry.SLEEP", lambda s: None)
    w["out"] = EXP.ExpiryDecision(action="extend", extend_days=2, rationale="趋势仍在")

    def _tm(cfg, prompt):
        w["tm"].append(prompt)
        if isinstance(w["out"], Exception):
            raise w["out"]
        return w["out"]
    monkeypatch.setattr(EXP, "_call_tm", _tm)
    cfg = settings.load_config()
    cfg["expiry_extend"]["mode"] = "simple"   # 本文件测 62648d3 简化问答（simple 模式）；全员讨论见 test_expiry_full_discussion.py
    cfg["expiry_extend"]["max_extensions"] = 1
    w["cfg"], w["run"] = cfg, (lambda: demo_watch.run_once(w["cfg"]))
    return w


def _events(w):
    return [json.loads(l) for l in (w["tmp"] / "ev.jsonl").read_text().splitlines()] if (w["tmp"] / "ev.jsonl").exists() else []


def test_config_defaults():
    c = settings.load_config()["expiry_extend"]
    assert c["enabled"] is True and c["max_extend_days"] == 3 and c["max_extensions"] == 2 and c["mode"] == "full_discussion"


def test_expiry_extended(world):
    old = world["store"]["reg"]["positions"]["BTC-USDT-SWAP"]["deadline"]
    world["run"]()
    m = world["store"]["reg"]["positions"]["BTC-USDT-SWAP"]
    assert world["close"] == [] and m["extensions"] == 1 and m["orig_deadline"] == old and m["extended_days"] == 2
    assert EXP._dt.datetime.strptime(m["deadline"], EXP.FMT) - EXP._dt.datetime.strptime(old, EXP.FMT) == dt.timedelta(days=2)
    assert [e["kind"] for e in world["sent"]] == ["position_extended"]
    assert [e["event"] for e in _events(world)].count("expiry_extended") == 1
    assert "入场均价 100.0" in world["tm"][0] and "止损 95.0" in world["tm"][0] and "已延期 0 次" in world["tm"][0]
    # 再跑一轮（未到新 deadline）：不再调用交易经理、不再通知
    world["run"]()
    assert len(world["tm"]) == 1 and len(world["sent"]) == 1 and world["redis"] == 0


def test_extend_days_capped(world):
    world["out"] = EXP.ExpiryDecision(action="extend", extend_days=10, rationale="x")
    world["run"]()
    assert world["store"]["reg"]["positions"]["BTC-USDT-SWAP"]["extended_days"] == 3


def test_expiry_closed_by_tm(world):
    world["out"] = EXP.ExpiryDecision(action="close", rationale="动能衰竭")
    world["run"]()
    assert world["close"] == ["持有期到期"] and [e["kind"] for e in world["sent"]] == ["position_expired"]
    ev = [e for e in _events(world) if e["event"] == "expiry_closed"]
    assert len(ev) == 1 and "动能衰竭" in ev[0]["reason"] and world["redis"] == 0


def test_extend_rejected_when_consensus_disagrees(world):
    world["S"] = -0.3
    world["run"]()
    assert world["close"] == ["持有期到期"] and "共识" in world["sent"][0]["message"]


def test_cap_reached_closes_without_tm(world):
    m = world["store"]["reg"]["positions"]["BTC-USDT-SWAP"]
    m.update(extensions=1, extended_days=2, orig_deadline=_ts(days=-2))
    world["run"]()
    assert world["tm"] == [] and world["close"] == ["持有期到期"] and "上限" in world["sent"][0]["message"]


def test_tm_failure_falls_back_to_close(world, monkeypatch):
    """_call_tm 真实路径：invoke 一直失败 → llm_retry 重试 3 次（共 4 次）仍失败 → 回退到期平仓。"""
    calls = []

    class Boom:
        def invoke(self, p):
            calls.append(1); raise ValueError("bad")
    monkeypatch.setattr(EXP, "_call_tm", lambda cfg, prompt: __import__("desk.llm_retry", fromlist=["x"]).call(
        Boom().invoke, prompt, delays=EXP.ecfg(cfg)["retry_delays"], any_error=True))
    world["run"]()
    assert len(calls) == 4 and world["close"] == ["持有期到期"] and "调用失败" in world["sent"][0]["message"]


def test_halt_no_close_no_tm(world, monkeypatch):
    monkeypatch.setattr(settings, "halt_active", lambda: True)
    world["run"]()
    assert world["close"] == [] and world["tm"] == [] and world["sent"] == []
    assert any(e["event"] == "halt_skip_close" for e in _events(world))


def test_restart_does_not_rejudge(world):
    """结论已按当期 deadline 落盘（例如平仓下单前进程重启）→ 下一进程直接执行结论，不再调用交易经理。"""
    m = world["store"]["reg"]["positions"]["BTC-USDT-SWAP"]
    m["expiry_verdict"] = {"deadline": m["deadline"], "action": "close", "days": 0, "reason": "交易经理选择平仓：旧结论"}
    world["run"]()
    assert world["tm"] == [] and world["close"] == ["持有期到期"] and len(world["sent"]) == 1


def test_round_timeouts_fall_back(world):
    m = world["store"]["reg"]["positions"]["BTC-USDT-SWAP"]
    m["expiry_attempts"] = {m["deadline"]: 3}   # 前 3 轮调用都被轮次超时打断
    world["run"]()
    assert world["tm"] == [] and world["close"] == ["持有期到期"]


def test_pre_window_close_waits_until_deadline(world):
    world["store"]["reg"]["positions"]["BTC-USDT-SWAP"]["deadline"] = _ts(minutes=3)
    world["out"] = EXP.ExpiryDecision(action="close", rationale="到期就平")
    world["run"]()
    assert world["close"] == [] and len(world["tm"]) == 1 and world["sent"] == []
    world["run"]()
    assert len(world["tm"]) == 1      # 已有结论，不重复调用


def test_disabled_old_behavior(world):
    world["cfg"]["expiry_extend"]["enabled"] = False
    world["run"]()
    assert world["tm"] == [] and world["close"] == ["持有期到期"]


def test_report_lines(world):
    world["run"]()
    lines = EXP.report_lines(settings.now_str()[:10])
    assert len(lines) == 1 and lines[0].startswith("- 持有期到期：BTC-USDT-SWAP 延期 2 天") and "趋势仍在" in lines[0]
