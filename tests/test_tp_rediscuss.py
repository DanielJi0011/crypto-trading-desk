"""止盈成交也触发全角色再讨论（2026-10-24 用户同意）：与止损同一流程与护栏，止盈不记止损冷却。离线，不下单，不联网。"""
import json

from desk import demo_ws as W, monitor, settings, rediscuss
from desk import okx_demo as X
from test_r12_min import base, _watch  # noqa: F401  复用 demo_watch 离线夹具


def _algo(aid, act):
    return {"arg": {"channel": "orders-algo", "instType": "SWAP"}, "data": [{
        "instType": "SWAP", "instId": "BTC-USDT-SWAP", "algoId": aid, "ordId": "o" + aid, "ordType": "oco",
        "state": "effective", "side": "buy", "posSide": "short", "sz": "1", "actualSz": "1", "actualPx": "79750",
        "actualSide": act, "triggerPx": "79750"}]}


def _no_cooldown(monkeypatch):
    from desk import stop_cooldown
    rec = []
    monkeypatch.setattr(stop_cooldown, "record", lambda *a, **k: rec.append(a))
    return rec


def test_ws_tp_triggers_rediscuss_once_without_stop_cooldown(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "EVENTS", tmp_path / "ev.jsonl")
    rec = _no_cooldown(monkeypatch)
    calls = []
    st = W.State(tmp_path / "seen.json")
    for _ in range(3):
        W.handle(W.parse(_algo("901", "tp"))[0], st, cfg={}, rediscuss=lambda c, t: calls.append(t) or "已启动", notifier=lambda e: None)
    assert calls == [{"inst_id": "BTC-USDT-SWAP", "trade_id": "DEMO-901", "reason": "tp"}]
    assert rec == []   # 止盈不记止损冷却
    ev = json.loads((tmp_path / "ev.jsonl").read_text().splitlines()[0])
    assert ev["kind"] == "take_profit" and ev["rediscuss_reason"] == "tp" and ev["rediscuss"] == "已启动"


def test_ws_sl_still_records_cooldown(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "EVENTS", tmp_path / "ev.jsonl")
    rec = _no_cooldown(monkeypatch)
    W.handle(W.parse(_algo("902", "sl"))[0], W.State(tmp_path / "s.json"), cfg={}, rediscuss=lambda c, t: "已启动", notifier=lambda e: None)
    assert len(rec) == 1


def test_ws_tp_drill_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "EVENTS", tmp_path / "ev.jsonl")
    monkeypatch.setattr(W, "_drill", lambda inst: True)
    calls = []
    out = W.handle(W.parse(_algo("903", "tp"))[0], W.State(tmp_path / "s.json"), cfg={},
                   rediscuss=lambda c, t: calls.append(t) or "已启动", notifier=lambda e: None)
    assert calls == [] and out["rediscuss"] == "演练跳过"


def test_schedule_tp_dedup_cooldown_and_reason(monkeypatch):
    spawned = []
    monkeypatch.setattr(monitor, "SPAWN", lambda a, lf: spawned.append(a))
    cfg = settings.load_config()
    t = {"inst_id": "SOL-USDT-SWAP", "trade_id": "DEMO-910", "reason": "tp"}
    assert monitor.schedule_rediscuss(cfg, t) == "已启动"
    assert monitor.schedule_rediscuss(cfg, dict(t)) == "该事件已讨论过"   # 每笔止盈只讨论一次
    assert monitor.schedule_rediscuss(cfg, {**t, "trade_id": "DEMO-911"}) == "冷却中"   # 6 小时冷却沿用
    assert spawned == [["--coin", "SOL", "--event", "DEMO-910", "--reason", "tp"]]
    ev = [json.loads(l) for l in X.EVENTS.read_text().splitlines()] if hasattr(X, "EVENTS") and X.EVENTS.exists() else []
    if ev:
        assert any(e.get("event") == "rediscuss_started" and e.get("reason") == "tp" for e in ev)


def test_reason_brief_warns_against_chasing():
    b = rediscuss.reason_brief("tp", "DEMO-1")
    assert "止盈" in b and "追单" in b and "回抽" in b
    assert "止损" in rediscuss.reason_brief("sl", "DEMO-2")


def test_rediscuss_halt_blocks(monkeypatch, capsys):
    monkeypatch.setattr(settings, "halt_active", lambda: True)
    assert rediscuss.main(["--coin", "BTC", "--event", "DEMO-1", "--reason", "tp", "--mock"]) == 0
    assert "HALT" in capsys.readouterr().out


def _watch_tp(monkeypatch, tmp, side, drill=False, halt=False):
    reg = {"triggers": {}, "positions": {}, "watch": {"last_algo_uts": 1}}
    w = _watch(monkeypatch, tmp, reg, "live")
    base_get = X.get
    hist = [{"instId": "BTC-USDT-SWAP", "algoId": "920", "actualSide": side, "uTime": "5", "side": "buy", "posSide": "short", "actualPx": "79750"}]
    monkeypatch.setattr(X, "get", lambda path, **kw: {"data": hist} if path == "/api/v5/trade/orders-algo-history" and kw.get("ordType") == "oco" else base_get(path, **kw))
    calls = []
    monkeypatch.setattr(monitor, "schedule_rediscuss", lambda cfg, t: calls.append(t) or "已启动")
    from desk import drill as D
    monkeypatch.setattr(D, "active", lambda inst: drill)
    if halt:
        monkeypatch.setattr(settings, "halt_active", lambda: True)
    return w, calls, _no_cooldown(monkeypatch)


def test_watch_tp_triggers_rediscuss(base, monkeypatch):
    cfg, tmp = base
    w, calls, rec = _watch_tp(monkeypatch, tmp, "tp")
    w.run_once(cfg)
    assert calls == [{"inst_id": "BTC-USDT-SWAP", "trade_id": "DEMO-920", "reason": "tp"}] and rec == []


def test_watch_tp_drill_skip(base, monkeypatch):
    cfg, tmp = base
    w, calls, _ = _watch_tp(monkeypatch, tmp, "tp", drill=True)
    w.run_once(cfg)
    assert calls == []
