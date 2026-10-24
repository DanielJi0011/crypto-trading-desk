"""submit_trigger（固定张数）与 scripts/reprice_triggers_limit.py（全部 mock，不连网、不下单）。"""
import importlib.util
from pathlib import Path

import pytest

from desk import demo_exec as DX, entry_limit as EL, okx_demo as X, settings

INST = "SOL-USDT-SWAP"
INSTR = {"ctVal": "1", "lotSz": "0.01", "minSz": "0.01", "tickSz": "0.01"}
spec = importlib.util.spec_from_file_location("reprice", Path(__file__).resolve().parents[1] / "scripts" / "reprice_triggers_limit.py")
RP = importlib.util.module_from_spec(spec); spec.loader.exec_module(RP)


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(EL, "PATH", tmp_path / "el.json")
    monkeypatch.setattr(EL, "LOCK", tmp_path / "el.lock")
    monkeypatch.setattr(X, "EVENTS", tmp_path / "ev.jsonl")
    monkeypatch.setattr(DX, "REG", tmp_path / "reg.json")
    monkeypatch.setattr(DX, "_require", lambda: None)
    monkeypatch.setattr(DX, "_ps", lambda side: "short")
    monkeypatch.setattr(DX, "_set_lev", lambda *a: None)
    monkeypatch.setattr(X, "instrument", lambda inst: INSTR)
    cfgp = tmp_path / "config.yaml"
    cfgp.write_text('execution:\n  protected_algo_ids: ["OLD"]   # 旧注释 4.79 张\n')
    monkeypatch.setattr(RP, "CONFIG", cfgp)
    old = {"algoId": "OLD", "instId": INST, "side": "sell", "posSide": "short", "sz": "97.75", "triggerPx": "108.6", "ordPx": "-1",
           "lever": "1", "attachAlgoOrds": [{"slTriggerPx": "111.91", "tpTriggerPx": "100.1"}]}
    DX.save_reg({"triggers": {"OLD": {"side": "short", "trigger_px": 108.6, "stop_loss": 111.913, "take_profit": 100.095,
                                      "sz": "4.79", "leverage": 1.0, "decision_id": "D-SOL", "expires": "2026-10-27 02:27:14+0800"}},
                 "positions": {}, "watch": {}})
    st = {"pending": [old], "posts": [], "fail_limit": False}

    def post(path, body):
        st["posts"].append((path, body))
        if path == "/api/v5/trade/cancel-algos":
            st["pending"] = [t for t in st["pending"] if t["algoId"] != body[0]["algoId"]]; return {"data": [{}]}
        if path == "/api/v5/trade/order-algo":
            if st["fail_limit"] and body["orderPx"] != "-1":
                raise X.DemoError("POST order-algo code=51000 msg=mock fail")
            return {"data": [{"algoId": "NEW" if body["orderPx"] != "-1" else "NEWMKT"}]}
        raise AssertionError(path)

    def get(path, **kw):
        if path == "/api/v5/trade/orders-algo-history":
            return {"data": [{"algoId": kw["algoId"], "state": "canceled"}]}
        raise AssertionError(path)
    monkeypatch.setattr(X, "post", post); monkeypatch.setattr(X, "get", get)
    monkeypatch.setattr(DX, "trigger_orders", lambda inst=None: list(st["pending"]))
    monkeypatch.setattr(RP, "guard", lambda cfg, p: [])
    cfg = settings.load_config()
    cfg["execution"]["protected_algo_ids"] = ["OLD"]
    cfg["execution"]["entry_limit"] = {**cfg["execution"]["entry_limit"], "enabled": True}
    return cfg, st, cfgp


def _algo_posts(st):
    return [b for p, b in st["posts"] if p == "/api/v5/trade/order-algo"]


def test_reprice_keeps_size_and_sltp_and_limit(env):
    cfg, st, cfgp = env
    assert RP.run(cfg, "OLD", {"OLD"}, True) == "NEW"
    b = _algo_posts(st)[0]
    assert b["sz"] == "97.75" and b["triggerPx"] == "108.6" and b["orderPx"] == "108.71"   # 交易所实际张数，不按权益重算
    assert b["attachAlgoOrds"][0]["slTriggerPx"] == "111.91" and b["attachAlgoOrds"][0]["tpTriggerPx"] == "100.1"
    r = DX.reg()["triggers"]
    assert "OLD" not in r and r["NEW"]["expires"] == "2026-10-27 02:27:14+0800" and r["NEW"]["replaced_from"] == "OLD"
    assert EL._load()["entries"]["NEW"]["status"] == "armed"
    assert '["NEW"]' in cfgp.read_text() and "OLD" in cfgp.read_text().split("#", 1)[1]


def test_protected_not_replaced_without_flag(env):
    cfg, st, _ = env
    assert RP.run(cfg, "OLD", set(), True) is None and st["posts"] == []


def test_guard_reject_does_not_cancel(env, monkeypatch):
    cfg, st, _ = env
    monkeypatch.setattr(RP, "guard", lambda cfg, p: ["[MAX_ORDERS] x"])
    assert RP.run(cfg, "OLD", {"OLD"}, True) is None and st["posts"] == []


def test_limit_fail_falls_back_to_market_same_params(env):
    cfg, st, _ = env
    st["fail_limit"] = True
    assert RP.run(cfg, "OLD", {"OLD"}, True) == "NEWMKT"
    b = _algo_posts(st)[-1]
    assert b["orderPx"] == "-1" and b["sz"] == "97.75" and b["triggerPx"] == "108.6"
    assert "mock fail" in DX.reg()["triggers"]["NEWMKT"]["fallback_reason"]
    assert "NEWMKT" not in EL._load()["entries"]


def test_dry_run_no_posts(env):
    cfg, st, _ = env
    assert RP.run(cfg, "OLD", {"OLD"}, False) is None and st["posts"] == []
