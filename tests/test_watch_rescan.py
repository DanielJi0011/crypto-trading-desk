"""demo_watch 回扫 24 小时止损/止盈（按 algoId 去重）、demo_ws 子进程补查、旧代码告警、推送防御（离线）。"""
import time
from types import SimpleNamespace as NS

from desk import demo_ws as W, monitor, settings, demo_exec as DX
from desk import okx_demo as X
from test_r12_min import base, _watch  # noqa: F401
from test_tp_rediscuss import _no_cooldown


def _setup(monkeypatch, tmp, reg, hist, drill=False):
    w = _watch(monkeypatch, tmp, reg, "live")
    base_get = X.get
    monkeypatch.setattr(X, "get", lambda path, **kw: {"data": hist} if path == "/api/v5/trade/orders-algo-history" and kw.get("ordType") == "oco" else base_get(path, **kw))
    calls = []
    monkeypatch.setattr(monitor, "schedule_rediscuss", lambda cfg, t: calls.append(t["trade_id"]) or "已启动")
    from desk import drill as D
    monkeypatch.setattr(D, "active", lambda inst: drill)
    _no_cooldown(monkeypatch)
    return w, calls


def _h(aid, side, u):
    return {"instId": "SOL-USDT-SWAP", "algoId": aid, "actualSide": side, "uTime": str(u), "side": "buy", "posSide": "short", "actualPx": ""}


def test_first_deploy_marks_old_and_rescan_catches_swallowed(base, monkeypatch):
    cfg, tmp = base
    now = int(time.time() * 1000)
    # 旧事件 A（水位以内）上线时一次性标为已处理；B 在水位以内但已有 handled_algos 之后被旧进程推过水位 → 回扫补上
    hist = [_h("A", "tp", now - 3600_000)]
    reg = {"triggers": {}, "positions": {}, "watch": {"last_algo_uts": now - 1000}}
    w, calls = _setup(monkeypatch, tmp, reg, hist)
    w.run_once(cfg)
    assert calls == [] and "A" in DX.reg()["watch"]["handled_algos"]
    hist.append(_h("B", "tp", now - 600_000))   # 旧代码进程已把水位推过 B
    w.run_once(cfg)
    assert calls == ["DEMO-B"]
    w.run_once(cfg)
    assert calls == ["DEMO-B"]   # 去重：不重复触发


def test_rescan_window_and_drill(base, monkeypatch):
    cfg, tmp = base
    now = int(time.time() * 1000)
    hist = [_h("OLD", "sl", now - 30 * 3600_000), _h("NEW", "sl", now - 60_000)]
    reg = {"triggers": {}, "positions": {}, "watch": {"last_algo_uts": now, "handled_algos": {}}}
    w, calls = _setup(monkeypatch, tmp, reg, hist)
    w.run_once(cfg)
    assert calls == ["DEMO-NEW"]   # 超过 24 小时不回扫
    tmp2 = tmp / "d"; tmp2.mkdir()
    reg2 = {"triggers": {}, "positions": {}, "watch": {"last_algo_uts": now, "handled_algos": {}}}
    w, calls = _setup(monkeypatch, tmp2, reg2, [_h("DR", "tp", now - 60_000)], drill=True)
    w.run_once(cfg)
    assert calls == []
    w.run_once(cfg)
    assert calls == []   # 演练单记为已处理，不反复检测


def test_catchup_uses_subprocess(monkeypatch):
    got = []
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda a, **k: got.append((a, k)) or NS(returncode=0, stdout="ok", stderr=""))
    W.catchup_subprocess()
    a, k = got[0]
    assert a[1:4] == ["-m", "desk.demo_watch", "--once"] and k["cwd"] == str(settings.CODE) and k["timeout"]


def test_stale_code_alert_hourly(monkeypatch):
    monkeypatch.setattr(W, "_code_rev", {"start": None, "alerted": 0.0})
    sent = []
    assert not W.check_code_version(now=1000, head="aaa", notifier=sent.append)
    assert not W.check_code_version(now=1100, head="aaa", notifier=sent.append)
    assert W.check_code_version(now=5000, head="bbb", notifier=sent.append)
    assert W.check_code_version(now=6000, head="bbb", notifier=sent.append)
    assert len(sent) == 1 and sent[0]["kind"] == "stale_code" and "stale_code" in W.NOTIFY_KINDS
    W.check_code_version(now=5000 + 3600, head="bbb", notifier=sent.append)
    assert len(sent) == 2


def test_parse_defensive():
    assert W.parse({"arg": {"channel": "orders"}, "data": ["x", 1]}) == []
    ev = W.parse({"arg": {"channel": "orders-algo"}, "data": {"state": "effective", "actualSide": "tp", "algoId": "9", "instId": "SOL-USDT-SWAP"}})
    assert ev and ev[0]["kind"] == "take_profit"
    assert W.parse({"arg": {"channel": "balance_and_position"}, "data": [{"eventType": "liquidation", "posData": ["x"]}]}) == []
