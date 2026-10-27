"""分析→执行价格漂移检查（drift_check）：新开仓/加仓超阈值跳过；阈值内正常；持有/平仓/止损调整不受影响；
开关关闭不影响；取现价失败不阻断；决策记录与日报含字段。离线。"""
import types

import pytest

from desk import demo_exec as DX, drift, manager as MG, okx_data, okx_demo as X, settings, equity_guard as EG
from desk import report

INSTR = {"ctVal": "0.01", "lotSz": "0.01", "minSz": "0.01", "tickSz": "0.1"}
INST = "BTC-USDT-SWAP"
REF = {"px": 80000.0, "time": "2026-10-28 08:00:05"}


@pytest.fixture
def base(monkeypatch, tmp_path):
    monkeypatch.setattr(X, "_inst_cache", {INST: INSTR})
    monkeypatch.setattr(X, "EVENTS", tmp_path / "ev.jsonl")
    monkeypatch.setattr(EG, "MARKS", tmp_path / "m.json")
    monkeypatch.setattr(DX, "REG", tmp_path / "reg.json")
    monkeypatch.setattr(DX, "today_realized", lambda: 0.0); monkeypatch.setattr(DX, "today_orders", lambda: 0)
    cfg = settings.load_config()
    cfg["drift_check"] = {"enabled": True, "max_pct": 1.0}
    return cfg


def _events(tmp_path):
    p = tmp_path / "ev.jsonl"
    return [e["kind"] if "kind" in e else e.get("event") for e in settings.read_jsonl(p)] if p.exists() else []


def _px(monkeypatch, v):
    def f(inst):
        if isinstance(v, Exception):
            raise v
        return v
    monkeypatch.setattr(okx_data, "last_price", f)


def _short_pos():
    qty, px = 0.0604, 80000.0
    return {"inst_id": INST, "side": "short", "pos_side": "short", "qty": qty, "contracts": 6.04, "avg_px": 80760.4,
            "price": px, "sl_px": 82600.0, "tp_px": 77000.0, "leverage": 3.0, "margin": qty * px / 3, "notional": qty * px, "upnl": 0.0}


def _bk(positions):
    return {"equity": 100000.0, "live_equity": 100000.0, "positions": positions, "triggers": [], "pending_entries": [],
            "exposure": sum(p["notional"] for p in positions), "cash": 100000.0}


def _run(monkeypatch, cfg, *, pos=None, condition="now", tm_action="short", side="short", buy=True, ref=REF):
    calls = {"open": 0, "trig": 0, "add_now": 0, "add_trig": 0, "close": 0, "reduce": 0, "cancel": []}
    cfg = {**cfg, "risk": {**cfg["risk"], "allow_add_position": True}}
    agg = {"S": -0.5 if side == "short" else 0.5, "buy": buy, "side": side, "entry_px": 80000.0, "condition": condition,
           "stop_loss": 82000.0, "take_profit": 76000.0, "size_pct": 10, "horizon_days": 5, "notes": [], "plan_conflict": False}
    monkeypatch.setattr(MG, "aggregate", lambda c, st, w, mk: dict(agg))
    monkeypatch.setattr(MG, "pre_trade_gate", lambda c, inst, mk: None)
    monkeypatch.setattr(MG, "_cooldown", lambda *a: False)
    monkeypatch.setattr(MG, "demo_size", lambda *a: {"ok": True, "risk_usd": 750.0, "notional": 10000.0, "clipped": [],
                                                     "leverage": 1.0, "margin": 10000.0, "stop_loss_usd": 750.0})
    monkeypatch.setattr(DX, "cancel_triggers", lambda c, inst, why: calls["cancel"].append(why) or [])
    monkeypatch.setattr(DX, "open_now", lambda *a, **k: calls.__setitem__("open", calls["open"] + 1) or (True, {"sz": "1"}))
    monkeypatch.setattr(DX, "place_trigger", lambda *a, **k: calls.__setitem__("trig", calls["trig"] + 1) or (True, {"algoId": "A1", "sz": "1"}))
    monkeypatch.setattr(DX, "add_now", lambda *a, **k: calls.__setitem__("add_now", calls["add_now"] + 1) or (True, {"sz": a[4]}))
    monkeypatch.setattr(DX, "place_add_trigger", lambda *a, **k: calls.__setitem__("add_trig", calls["add_trig"] + 1) or (True, {"algoId": "T1", "sz": a[5]}))
    monkeypatch.setattr(DX, "close", lambda c, p, why: calls.__setitem__("close", calls["close"] + 1) or {"type": "close", "ok": True})
    monkeypatch.setattr(DX, "reduce", lambda c, p, f, why: calls.__setitem__("reduce", calls["reduce"] + 1) or {"type": "reduce", "ok": True})
    monkeypatch.setattr(DX, "reg", lambda: {"positions": {}, "triggers": {}})
    monkeypatch.setattr(DX, "save_reg", lambda r: None)
    tm = types.SimpleNamespace(action=tm_action, rationale="r", entry_expire_hours=None, entry_reason="", reduce_fraction=0.5)
    mk = {"last": 80000.0, "atr14": 2500.0, "bar_date": None}
    if ref:
        mk["_analysis_ref"] = ref
    res = MG._pm_execute_demo_locked(cfg, {"inst_id": INST}, "D-T", {}, {}, mk, _bk([pos] if pos else []), pos, "r",
                                     tm=tm, tm_state="go")
    return res, calls


# ---------------------------------------------------------------- 超阈值：新开仓/条件单/加仓跳过
@pytest.mark.parametrize("cond,key", [("now", "open"), ("lte", "trig")])
def test_open_skipped_over_threshold(base, monkeypatch, tmp_path, cond, key):
    _px(monkeypatch, 81000.0)   # +1.25%
    res, c = _run(monkeypatch, base, condition=cond)
    assert c[key] == 0 and res["action"] == "hold(价格漂移)"
    d = res["aggregate"]["drift_skipped"]
    assert d["analysis_px"] == 80000.0 and d["now_px"] == 81000.0 and d["deviation_pct"] == pytest.approx(1.25) and d["max_pct"] == 1.0
    assert "drift_skipped" in _events(tmp_path)
    assert any("价格漂移跳过开仓" in n for n in res["notes"])


@pytest.mark.parametrize("cond,key", [("now", "add_now"), ("gte", "add_trig")])
def test_add_skipped_over_threshold(base, monkeypatch, tmp_path, cond, key):
    _px(monkeypatch, 79000.0)   # -1.25%
    res, c = _run(monkeypatch, base, pos=_short_pos(), condition=cond)
    assert c[key] == 0 and res["action"] == "hold(价格漂移)"
    assert res["aggregate"]["drift_skipped"]["leg"] == "add"
    assert res["aggregate"]["drift_skipped"]["deviation_pct"] == pytest.approx(-1.25)
    assert "drift_skipped" in _events(tmp_path)


# ---------------------------------------------------------------- 阈值内正常
def test_within_threshold_open_and_add(base, monkeypatch, tmp_path):
    _px(monkeypatch, 80700.0)   # +0.875%
    res, c = _run(monkeypatch, base)
    assert c["open"] == 1 and res["action"] == "short" and res["aggregate"]["drift_check"]["ok"] is True
    assert "drift_skipped" not in res["aggregate"]
    res, c = _run(monkeypatch, base, pos=_short_pos())
    assert c["add_now"] == 1 and res["action"] == "add_short"
    assert "drift_skipped" not in _events(tmp_path)


# ---------------------------------------------------------------- 持有/平仓/减仓/止损调整不受影响
@pytest.mark.parametrize("act,key", [("sell", "close"), ("reduce", "reduce")])
def test_close_reduce_unaffected(base, monkeypatch, act, key):
    _px(monkeypatch, 90000.0)   # +12.5%
    res, c = _run(monkeypatch, base, pos=_short_pos(), tm_action=act)
    assert c[key] == 1 and res["action"] == act and "drift_check" not in res["aggregate"]


def test_hold_unaffected(base, monkeypatch):
    _px(monkeypatch, 90000.0)
    res, c = _run(monkeypatch, base, buy=False)
    assert res["action"] == "hold" and "drift_check" not in res["aggregate"]
    res, c = _run(monkeypatch, base, pos=_short_pos(), buy=False, tm_action="hold")
    assert res["action"] == "hold" and "drift_check" not in res["aggregate"]


def test_sl_adjust_not_gated():
    """止损止盈调整走 apply_position_sl_tp（执行锁外），不调用 drift。"""
    import inspect
    for fn in (MG.apply_position_sl_tp, MG.apply_new_stop, MG.apply_new_tp):
        assert "drift" not in inspect.getsource(fn)


# ---------------------------------------------------------------- 开关关闭 / 无参考价
def test_disabled_no_effect(base, monkeypatch, tmp_path):
    _px(monkeypatch, 90000.0)
    base["drift_check"] = {"enabled": False, "max_pct": 1.0}
    res, c = _run(monkeypatch, base)
    assert c["open"] == 1 and "drift_check" not in res["aggregate"]
    res, c = _run(monkeypatch, base, pos=_short_pos())
    assert c["add_now"] == 1
    assert not _events(tmp_path)


def test_no_reference_no_check(base, monkeypatch):
    _px(monkeypatch, 90000.0)
    res, c = _run(monkeypatch, base, ref=None)
    assert c["open"] == 1 and "drift_check" not in res["aggregate"]


# ---------------------------------------------------------------- 取现价失败不阻断
def test_price_fetch_failure_does_not_block(base, monkeypatch, tmp_path):
    _px(monkeypatch, RuntimeError("okx down"))
    res, c = _run(monkeypatch, base)
    assert c["open"] == 1 and res["action"] == "short"
    assert res["aggregate"]["drift_check"]["ok"] is None
    assert "drift_check_price_unavailable" in _events(tmp_path)
    assert any("取现价失败" in n for n in res["notes"])


def test_stamp(monkeypatch, tmp_path):
    monkeypatch.setattr(X, "EVENTS", tmp_path / "ev.jsonl")
    _px(monkeypatch, 123.4)
    s = drift.stamp(INST)
    assert s["px"] == 123.4 and s["time"]
    _px(monkeypatch, RuntimeError("x"))
    assert drift.stamp(INST) is None and "drift_ref_unavailable" in _events(tmp_path)


def test_config_yaml_defaults():
    c = settings.load_config()
    assert c["drift_check"] == {"enabled": True, "max_pct": 1.0}
    assert drift.dcfg({}) == {"enabled": True, "max_pct": 1.0}


# ---------------------------------------------------------------- 决策记录 + 日报
def test_record_and_report(base, monkeypatch, tmp_path):
    _px(monkeypatch, 81000.0)
    monkeypatch.setattr(MG, "market_ctx", lambda inst: {"last": 80900.0, "ret_20d": 1.0, "ret_5d": 0.5, "atr14": 2500.0,
                                                        "close": 80900.0, "bar_date": None, "hi_20": 1, "lo_20": 1})
    monkeypatch.setattr(MG, "add_preview", lambda *a, **k: None)
    monkeypatch.setattr(MG, "role_texts", lambda fs: {})
    monkeypatch.setattr(MG, "DECISIONS", tmp_path / "dec.jsonl")
    seen = {}

    def fake_pm(cfg, coin, did, st, w, mk, bk, pos, rationale, source="daily", tm=None, tm_state=None):
        seen["mk"] = mk
        agg = {"S": 0.5, "notes": []}
        notes = []
        drift.skip(cfg, coin["inst_id"], did, mk, agg, notes, "open")
        return {"action": "hold(价格漂移)", "size_pct": 0.0, "notes": notes, "execution": None, "aggregate": agg, "pending": None}
    monkeypatch.setattr(MG, "pm_execute", fake_pm)
    coin = next(c for c in base["coins"] if c["inst_id"] == INST)
    from desk.roles import ROLE_KEYS
    st = {r: {"direction": "neutral", "confidence": 0.5, "source": "t"} for r in ROLE_KEYS}
    rec = MG.decide_and_execute(base, coin, "2026-10-28", {"_analysis_px": REF}, st, None)
    assert seen["mk"]["_analysis_ref"] == REF
    assert rec["analysis_px"] == 80000.0 and rec["analysis_px_time"] == REF["time"]
    assert rec["drift_skipped"]["now_px"] == 81000.0 and rec["drift_skipped"]["deviation_pct"] == pytest.approx(1.25)
    saved = settings.read_jsonl(tmp_path / "dec.jsonl")[-1]
    assert saved["drift_skipped"]["max_pct"] == 1.0 and saved["analysis_px"] == 80000.0
    usage = {"calls": 0, "errors": 0, "input": 0, "cached": 0, "output": 0, "cost_usd": 0, "by_stage": {}, "by_model": {}, "by_provider": {}}
    p = report.write(base, "2099-01-01", [rec], [], [], [], [], usage, {}, [], name="t-drift")
    txt = open(p, encoding="utf-8").read()
    assert "价格漂移跳过开仓" in txt and "80000" in txt and "81000" in txt and "+1.25%" in txt and "±1%" in txt
    assert "不下单（价格漂移超阈值）" in txt
