"""R11 确定性单号、下单意图记录与对账。离线：替换 okx_demo._request / get。"""
import datetime as dt

import pytest

from desk import demo_exec as DX, okx_demo as X, reconcile as RC, settings


@pytest.fixture(autouse=True)
def _tmp_intents(monkeypatch, tmp_path):
    monkeypatch.setattr(X, "INTENTS", tmp_path / "intents.jsonl")


def _intents():
    return settings.read_jsonl(X.INTENTS)


def test_det_id_stable_and_valid():
    a = DX.det_id("open", "BTC-USDT-SWAP", "d1", "long", "1", "2026-10-23 10:00")
    assert a == DX.det_id("open", "BTC-USDT-SWAP", "d1", "long", "1", "2026-10-23 10:00")
    assert a != DX.det_id("open", "BTC-USDT-SWAP", "d2", "long", "1", "2026-10-23 10:00")
    assert len(a) <= 32 and a.isalnum()


def test_intent_then_ack(monkeypatch):
    monkeypatch.setattr(X, "_request", lambda m, p, body=None, params=None: {"code": "0", "data": [{"ordId": "77"}]})
    X.post("/api/v5/trade/order", {"instId": "BTC-USDT-SWAP", "ordType": "market", "clOrdId": "taX1", "sz": "1"})
    assert [r["state"] for r in _intents() if r["cl"] == "taX1"] == ["intent", "ack"]
    assert _intents()[-1]["ordId"] == "77"


def test_intent_error_recorded(monkeypatch):
    def boom(*a, **k):
        raise X.DemoError("timeout")
    monkeypatch.setattr(X, "_request", boom)
    with pytest.raises(X.DemoError):
        X.post("/api/v5/trade/order-algo", {"instId": "ETH-USDT-SWAP", "ordType": "oco", "algoClOrdId": "taX2"})
    assert [r["state"] for r in _intents() if r["cl"] == "taX2"] == ["intent", "error"]


def test_no_intent_without_cl(monkeypatch):
    monkeypatch.setattr(X, "_request", lambda *a, **k: {"code": "0", "data": [{}]})
    X.post("/api/v5/trade/cancel-algos", [{"instId": "x", "algoId": "1"}])
    assert not [r for r in _intents()]


def _seed(cl, state, path="/api/v5/trade/order", mins=5):
    t = (settings.now() - dt.timedelta(minutes=mins)).strftime("%Y-%m-%d %H:%M:%S%z")
    settings.append_jsonl(X.INTENTS, {"time": t, "state": state, "cl": cl, "path": path, "inst_id": "BTC-USDT-SWAP"})


def test_reconcile_found_absent_and_skips(monkeypatch):
    _seed("taFOUND", "intent"); _seed("taGONE", "error", "/api/v5/trade/order-algo"); _seed("taOK", "intent"); _seed("taOK", "ack")
    _seed("taNEW", "intent", mins=0); _seed("taOLD", "intent", mins=60 * 30)
    alerts = []
    monkeypatch.setattr(DX, "_alert", lambda k, **kw: alerts.append(k))

    def get(path, **kw):
        if kw.get("clOrdId") == "taFOUND":
            return {"data": [{"ordId": "5", "state": "filled"}]}
        if kw.get("algoClOrdId") == "taGONE":
            raise X.DemoError("code=51603")
        raise AssertionError(kw)
    monkeypatch.setattr(X, "get", get)
    assert sorted(RC.run()) == [("taFOUND", "found"), ("taGONE", "absent")]
    assert alerts == ["intent_orphan_found"]
    assert RC.run() == []          # 已定论，不重复查


def test_reconcile_lookup_failure_retries(monkeypatch):
    _seed("taERR", "intent")
    monkeypatch.setattr(X, "get", lambda *a, **k: (_ for _ in ()).throw(X.DemoError("HTTP 500")))
    assert RC.run() == [("taERR", "unknown")]
    assert RC.pending()[0]["cl"] == "taERR"
