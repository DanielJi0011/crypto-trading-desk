"""决策 4（2026-11-15 用户批准）：exit_mode=trailing 的持仓到期时，最新存档行情判定（生效阶段）
仍为同向 strong/moderate 非 late_exhaustion 趋势 → 自动续一期（horizon_days），不触发到期复核；
判定转弱/衰竭/反向/失败、非 trailing 仓位、开关关、达总延期上限 → 原到期路径一行不改。"""
import datetime as dt
import json

import pytest

from desk import settings, okx_demo as X, demo_exec as DX, expiry as EXP
from desk import regime_judge as RJ

INST = "BTC-USDT-SWAP"


def _ts(**kw):
    return (settings.now() + dt.timedelta(**kw)).strftime(EXP.FMT)


def _rec(direction="short", strength="strong", stage="mid", failed=False, inst=INST, **jk):
    judge = None if failed else {
        "direction": direction, "strength": strength, "stage": stage,
        "stage_raw": jk.get("stage_raw", stage), "stage_overridden": jk.get("stage_overridden", False),
        "confidence": 0.8, "evidence": ["证据"], "summary": "趋势延续",
        "exhaustion_signals": jk.get("signals", [])}
    return {"time": settings.now_str(), "date": "2026-11-15", "session": "evening", "source": "daily",
            "inst_id": inst, "decision_id": "D-20261115-E-BTC", "judge_standard": 3,
            "judge": judge, "failed": failed,
            "active": bool(judge and strength in ("strong", "moderate")), "downgrade": False, "features": {}}


@pytest.fixture
def calls(monkeypatch, tmp_path):
    """隔离的 regime_calls.jsonl + 追加判定记录的入口。"""
    p = tmp_path / "regime_calls.jsonl"
    monkeypatch.setattr(RJ, "REGIME_CALLS", p)
    return lambda **kw: settings.append_jsonl(p, _rec(**kw))


@pytest.fixture
def world(monkeypatch, tmp_path, calls):
    from desk import demo_watch, entry_limit, demo_ws, monitor, trigger_review, manager
    from desk import trailing_stop, rr_recheck, stop_coverage, reconcile
    past = _ts(hours=-1)
    store = {"reg": {"triggers": {}, "positions": {INST: {
        "deadline": past, "opened": _ts(days=-5), "horizon_days": 5, "side": "short",
        "exit_mode": "trailing", "plan_tp": 80.0, "atr14_open": 2.0,
        "trail": {"activated": True, "best_px": 88.0, "stop_px": 95.0}}},
        "watch": {"last_algo_uts": 1}}}
    w = {"close": [], "sent": [], "tm": [], "store": store, "tmp": tmp_path, "add_verdict": calls}
    monkeypatch.setattr(X, "verify", lambda: True)
    monkeypatch.setattr(X, "EVENTS", tmp_path / "ev.jsonl")
    monkeypatch.setattr(trigger_review, "poll", lambda *a, **k: {})
    monkeypatch.setattr(entry_limit, "check_timeouts", lambda cfg: [])
    monkeypatch.setattr(trailing_stop, "check", lambda cfg: [])
    monkeypatch.setattr(rr_recheck, "check", lambda cfg, halt=False: [])
    monkeypatch.setattr(stop_coverage, "check", lambda cfg, halt=False: [])
    monkeypatch.setattr(reconcile, "run", lambda: [])
    monkeypatch.setattr(DX, "reg", lambda: json.loads(json.dumps(store["reg"])))
    monkeypatch.setattr(DX, "save_reg", lambda r: store.__setitem__("reg", json.loads(json.dumps(r))))
    monkeypatch.setattr(DX, "trigger_orders", lambda inst=None: [])
    monkeypatch.setattr(DX, "demo_positions", lambda: [])
    pos = {"inst_id": INST, "trade_id": "DEMO-BTC", "side": "short", "avg_px": 100.0, "price": 90.0,
           "upnl": 10.0, "sl_px": 95.0, "tp_px": None}
    monkeypatch.setattr(DX, "book", lambda cfg, prices=None: {"positions": [pos], "equity": 1, "live_equity": 1, "triggers": []})

    def _close(cfg, p, why):
        w["close"].append(why); store["reg"]["positions"].pop(p["inst_id"], None)
    monkeypatch.setattr(DX, "close", _close)
    monkeypatch.setattr(demo_ws, "sync_snapshot", lambda *a, **k: [])
    monkeypatch.setattr(demo_ws, "notify", lambda ev: w["sent"].append(ev))
    monkeypatch.setattr(X, "get", lambda path, **kw: {"data": []})
    monkeypatch.setattr(monitor, "schedule_rediscuss", lambda *a, **k: None)
    monkeypatch.setattr(manager, "market_ctx", lambda inst: {"last": 90.0})
    monkeypatch.setattr(EXP, "latest_decision", lambda inst: {"decision_id": "D-1", "consensus": -0.4, "stances": {}})
    monkeypatch.setattr("desk.llm_retry.SLEEP", lambda s: None)
    w["out"] = EXP.ExpiryDecision(action="close", rationale="到期平仓")

    def _tm(cfg, prompt):
        w["tm"].append(prompt)
        return w["out"]
    monkeypatch.setattr(EXP, "_call_tm", _tm)
    cfg = settings.load_config()
    cfg["expiry_extend"]["mode"] = "simple"   # 原路径用 simple 模式观察（TM 被调用与否一目了然）
    w["cfg"], w["run"] = cfg, (lambda: demo_watch.run_once(w["cfg"]))
    return w


def _events(w):
    return [json.loads(l) for l in (w["tmp"] / "ev.jsonl").read_text().splitlines()] if (w["tmp"] / "ev.jsonl").exists() else []


def _m(w):
    return w["store"]["reg"]["positions"][INST]


# ------------------------------------------------------------------ (a) 自动续期
def test_trailing_auto_extends_on_fresh_trend_verdict(world):
    old = _m(world)["deadline"]
    world["add_verdict"]()   # short / strong / mid
    world["run"]()
    m = _m(world)
    assert world["close"] == [] and world["tm"] == []          # 没平仓、没走复核（TM 未被调用）
    assert m["extensions"] == 1 and m["orig_deadline"] == old
    delta = EXP._dt.datetime.strptime(m["deadline"], EXP.FMT) - EXP._dt.datetime.strptime(old, EXP.FMT)
    assert delta == dt.timedelta(days=5)                        # 续一期 = horizon_days
    ev = [e for e in _events(world) if e["event"] == "horizon_extended_trailing"]
    assert len(ev) == 1 and ev[0]["days"] == 5 and ev[0]["new_deadline"] == m["deadline"]
    assert ev[0]["verdict"]["stage"] == "mid" and ev[0]["verdict"]["decision_id"] == "D-20261115-E-BTC"
    assert [e["kind"] for e in world["sent"]] == ["position_extended"] and "自动续期" in world["sent"][0]["message"]
    world["run"]()                                             # 未到新 deadline：不再续、不再通知
    assert len([e for e in _events(world) if e["event"] == "horizon_extended_trailing"]) == 1
    assert len(world["sent"]) == 1 and _m(world)["extensions"] == 1


def test_second_expiry_rechecks_latest_verdict(world):
    """续期不是永久通行证：下一期到期时重新读最新判定，已转 late_exhaustion → 走原路径平仓。"""
    # simple 模式另有累计天数上限 max_extend_days=3 < 一期 horizon 5 天：自动续期经共用的 apply_extend
    # 累计 extended_days 后，simple 路径会先撞天数上限直接平仓（TM 不被咨询）。本用例要观察的是
    # 「回落到原路径且 TM 被咨询」，故放宽该 legacy 上限；生产 full_discussion 模式只按次数上限，不受此影响。
    world["cfg"]["expiry_extend"]["max_extend_days"] = 30
    world["add_verdict"]()
    world["run"]()
    assert _m(world)["extensions"] == 1
    _m(world)["deadline"] = _ts(hours=-1)                       # 模拟下一期到期
    world["add_verdict"](stage="late_exhaustion", signals=["forced_flow_exhaustion"])
    world["run"]()
    assert world["close"] == ["持有期到期"] and len(world["tm"]) == 1
    assert len([e for e in _events(world) if e["event"] == "horizon_extended_trailing"]) == 1


# ------------------------------------------------------------------ (b) 判定不满足 → 原路径
def test_trailing_late_exhaustion_uses_normal_path(world):
    world["add_verdict"](stage="late_exhaustion", signals=["forced_flow_exhaustion"])
    world["run"]()
    assert world["close"] == ["持有期到期"] and len(world["tm"]) == 1     # TM 被咨询 = 原路径
    assert not [e for e in _events(world) if e["event"] == "horizon_extended_trailing"]
    assert [e["event"] for e in _events(world)].count("expiry_closed") == 1


def test_trailing_direction_flip_uses_normal_path(world):
    world["add_verdict"](direction="long")                     # 判定转多，持仓为空单
    world["out"] = EXP.ExpiryDecision(action="extend", extend_days=2, rationale="再看看")
    world["run"]()
    assert world["close"] == [] and len(world["tm"]) == 1
    assert _m(world)["extensions"] == 1 and _m(world)["extended_days"] == 2   # 原路径的 TM 延期
    assert [e["event"] for e in _events(world)].count("expiry_extended") == 1
    assert not [e for e in _events(world) if e["event"] == "horizon_extended_trailing"]


def test_trailing_switch_off_uses_normal_path(world):
    world["cfg"]["execution"]["trailing_horizon_extend"]["enabled"] = False
    world["add_verdict"]()
    world["run"]()
    assert world["close"] == ["持有期到期"] and len(world["tm"]) == 1
    assert not [e for e in _events(world) if e["event"] == "horizon_extended_trailing"]


def test_trailing_cap_reached_no_auto_extend(world):
    _m(world)["extensions"] = 2                                 # config max_extensions=2（所有模式共享总上限）
    world["add_verdict"]()
    world["run"]()
    assert world["close"] == ["持有期到期"] and world["tm"] == []          # 原路径同样因上限直接平仓
    assert not [e for e in _events(world) if e["event"] == "horizon_extended_trailing"]


# ------------------------------------------------------------------ (c) 非 trailing 仓位不受影响
def test_fixed_tp_position_with_trend_verdict_uses_normal_path(world):
    _m(world).pop("exit_mode")
    world["add_verdict"]()                                     # 同向强趋势判定也续不了固定止盈仓位
    world["run"]()
    assert world["close"] == ["持有期到期"] and len(world["tm"]) == 1
    assert not [e for e in _events(world) if e["event"] == "horizon_extended_trailing"]


# ------------------------------------------------------------------ 报表行
def test_report_lines_include_auto_extend(world):
    world["add_verdict"]()
    world["run"]()
    lines = EXP.report_lines(settings.now_str()[:10])
    auto = [l for l in lines if "移动止损仓位自动续期" in l]
    assert len(auto) == 1 and "续期 5 天" in auto[0] and "short/strong/mid" in auto[0]


# ------------------------------------------------------------------ 判定函数单元矩阵
def _unit_m(**kw):
    m = {"exit_mode": "trailing", "side": "short", "deadline": _ts(days=1), "horizon_days": 5, "extensions": 0}
    m.update(kw)
    return m


def test_check_cfg_defaults():
    assert EXP.trailing_horizon_cfg({}) == {"enabled": False}
    assert EXP.trailing_horizon_cfg(settings.load_config()) == {"enabled": True}


def test_check_matrix(calls):
    cfg = settings.load_config()
    calls()                                                     # short/strong/mid
    out = EXP.trailing_extend_check(cfg, INST, _unit_m(), "short")
    assert out["days"] == 5 and out["verdict"]["stage"] == "mid"
    assert EXP._dt.datetime.strptime(out["new_deadline"], EXP.FMT) - \
        EXP._dt.datetime.strptime(_unit_m()["deadline"], EXP.FMT) == dt.timedelta(days=5)
    calls(direction="short", strength="moderate", stage="early")
    assert EXP.trailing_extend_check(cfg, INST, _unit_m(), "short") is not None
    calls(direction="none", strength="none", stage="mid")
    assert EXP.trailing_extend_check(cfg, INST, _unit_m(), "short") is None
    calls(stage="late_exhaustion", signals=["structure_break"])
    assert EXP.trailing_extend_check(cfg, INST, _unit_m(), "short") is None
    # 生效阶段为准：模型原始 late、代码门强制降 mid（stage_overridden）→ 照续
    calls(stage="mid", stage_raw="late_exhaustion", stage_overridden=True)
    out = EXP.trailing_extend_check(cfg, INST, _unit_m(), "short")
    assert out is not None and out["verdict"]["stage_raw"] == "late_exhaustion"
    # 最新一场判定失败 → 不续（哪怕更早一场是好的）
    calls(failed=True)
    assert EXP.trailing_extend_check(cfg, INST, _unit_m(), "short") is None
    # 无任何存档判定 → 不续
    assert EXP.trailing_extend_check(cfg, "SOL-USDT-SWAP", _unit_m(), "short") is None
    # 非 trailing / 讨论已启动 / 开关关 → 不续
    calls()
    assert EXP.trailing_extend_check(cfg, INST, _unit_m(exit_mode=None), "short") is None
    m = _unit_m(); m["expiry_discussed_for"] = m["deadline"]
    assert EXP.trailing_extend_check(cfg, INST, m, "short") is None
    cfg2 = settings.load_config(); cfg2["execution"]["trailing_horizon_extend"]["enabled"] = False
    assert EXP.trailing_extend_check(cfg2, INST, _unit_m(), "short") is None
