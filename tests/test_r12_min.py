"""R12 最小部分：护栏读取 orders-pending；同币在途入场子单视同持仓；trigger_fired 与 entry_filled 分开；登记按子单终态清理。离线。"""
import datetime as dt
import json

import pytest

from desk import settings, demo_exec as DX, okx_demo as X, equity_guard as EG

INSTR = {"ctVal": "0.01", "lotSz": "0.01", "minSz": "0.01", "tickSz": "0.1"}
CHILD = {"ordId": "C7", "instId": "BTC-USDT-SWAP", "side": "buy", "posSide": "long", "reduceOnly": "false",
         "sz": "10", "accFillSz": "0", "px": "100000", "attachAlgoOrds": [{"slTriggerPx": "98000"}]}


@pytest.fixture
def base(monkeypatch, tmp_path):
    monkeypatch.setattr(X, "_inst_cache", {"BTC-USDT-SWAP": INSTR, "ETH-USDT-SWAP": INSTR})
    monkeypatch.setattr(X, "EVENTS", tmp_path / "ev.jsonl")
    monkeypatch.setattr(EG, "MARKS", tmp_path / "m.json")
    monkeypatch.setattr(DX, "today_realized", lambda: 0.0); monkeypatch.setattr(DX, "today_orders", lambda: 0)
    return settings.load_config(), tmp_path


def _bk(pend):
    return {"equity": 100000, "live_equity": 100000, "positions": [], "triggers": [], "exposure": 0, "pending_entries": pend}


def test_pending_risk_calc(base):
    cfg, _ = base
    r = DX.pending_entry_risk(cfg, [CHILD])[0]
    assert r["notional"] == pytest.approx(10000) and r["stop_risk"] == pytest.approx(200)


def test_same_coin_working_child_blocks(base):
    cfg, _ = base
    pend = DX.pending_entry_risk(cfg, [CHILD])
    r, ch = DX.check_open(cfg, _bk(pend), "BTC-USDT-SWAP", 0.01, 100000, 99000, "long", 1)
    assert any("[ONE_PER_COIN]" in x and "入场子单" in x for x in r)
    r, ch = DX.check_open(cfg, _bk(pend), "ETH-USDT-SWAP", 0.01, 2000, 1980, "long", 1)
    assert not any("[ONE_PER_COIN]" in x for x in r)
    assert ch["pending_entry_notional"] == pytest.approx(10000) and ch["pending_entry_stop_risk"] == pytest.approx(200)


def test_exposure_counts_child_notional(base):
    cfg, _ = base
    pend = DX.pending_entry_risk(cfg, [{**CHILD, "sz": "17900"}])     # 子单名义 1,790,000 → 总名义超 180%
    r, _ = DX.check_open(cfg, _bk(pend), "ETH-USDT-SWAP", 0.01, 2000, 1980, "long", 1)
    assert any("[EXPOSURE]" in x for x in r)


def test_missing_pending_view_fail_closed(base):
    cfg, _ = base
    bk = _bk([]); bk.pop("pending_entries")
    r, _ = DX.check_open(cfg, bk, "ETH-USDT-SWAP", 0.01, 2000, 1980, "long", 1)
    assert any("[GUARD_DATA]" in x for x in r)


def test_entry_limit_guard_blocks_other_child(base, monkeypatch):
    cfg, _ = base
    from desk import entry_limit as EL
    monkeypatch.setattr(DX, "usdt_equity", lambda: 100000.0)
    monkeypatch.setattr(DX, "pending_entry_orders", lambda inst=None: [CHILD])
    assert any("[ONE_PER_COIN]" in x for x in EL._guard(cfg, {"inst": "BTC-USDT-SWAP", "ordId": "OTHER"}))
    assert not any("[ONE_PER_COIN]" in x for x in EL._guard(cfg, {"inst": "BTC-USDT-SWAP", "ordId": "C7"}))


# ---------------- demo_watch
def _watch(monkeypatch, tmp_path, reg, order_state, algo_hist=None, pending=()):
    from desk import demo_watch, entry_limit, demo_ws, trigger_review
    monkeypatch.setattr(X, "verify", lambda: True)
    monkeypatch.setattr(trigger_review, "poll", lambda cfg: {"reasons": []})
    monkeypatch.setattr(entry_limit, "check_timeouts", lambda cfg: [])
    monkeypatch.setattr(DX, "reg", lambda: reg); monkeypatch.setattr(DX, "save_reg", lambda r: None)
    monkeypatch.setattr(DX, "trigger_orders", lambda inst=None: [])
    monkeypatch.setattr(DX, "demo_positions", lambda: [])
    monkeypatch.setattr(DX, "pending_entry_orders", lambda inst=None: list(pending))
    monkeypatch.setattr(DX, "book", lambda cfg, prices=None: {"positions": [], "equity": 1, "live_equity": 1, "triggers": []})
    monkeypatch.setattr(demo_ws, "sync_snapshot", lambda *a, **k: [])

    def get(path, **kw):
        if path == "/api/v5/trade/order":
            if order_state == "boom":
                raise X.DemoError("down")
            return {"data": [{"state": order_state, "accFillSz": "0" if order_state != "filled" else "10", "avgPx": "100"}]}
        if path == "/api/v5/trade/orders-algo-history" and kw.get("ordType") == "trigger":
            return {"data": algo_hist or []}
        return {"data": []}
    monkeypatch.setattr(X, "get", get)
    return demo_watch


def _events(tmp):
    p = tmp / "ev.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def test_effective_logs_trigger_fired_not_filled(base, monkeypatch):
    cfg, tmp = base
    reg = {"triggers": {"A1": {"inst_id": "BTC-USDT-SWAP", "side": "long", "horizon_days": 3}}, "positions": {}, "watch": {"last_algo_uts": 1}}
    w = _watch(monkeypatch, tmp, reg, "live", algo_hist=[{"state": "effective", "ordId": "C7"}])
    w.run_once(cfg)
    ev = [e["event"] for e in _events(tmp)]
    assert "trigger_fired" in ev and "trigger_filled" not in ev and "entry_filled" not in ev
    assert reg["positions"]["BTC-USDT-SWAP"]["child_ordId"] == "C7"     # 子单 live：登记保留


@pytest.mark.parametrize("state,kept", [("live", True), ("partially_filled", True), ("boom", True), ("canceled", False), ("filled", False)])
def test_registry_cleared_only_on_terminal(base, monkeypatch, state, kept):
    cfg, tmp = base
    old = (settings.now() - dt.timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S%z")   # 远超原 10 分钟规则
    reg = {"triggers": {}, "positions": {"BTC-USDT-SWAP": {"opened": old, "child_ordId": "C7"}}, "watch": {"last_algo_uts": 1}}
    w = _watch(monkeypatch, tmp, reg, state)
    w.run_once(cfg)
    assert ("BTC-USDT-SWAP" in reg["positions"]) is kept


def test_entry_filled_logged_once_with_size(base, monkeypatch):
    cfg, tmp = base
    reg = {"triggers": {}, "positions": {"BTC-USDT-SWAP": {"opened": settings.now_str(), "child_ordId": "C7"}}, "watch": {"last_algo_uts": 1}}
    w = _watch(monkeypatch, tmp, reg, "filled")
    w.run_once(cfg)
    f = [e for e in _events(tmp) if e["event"] == "entry_filled"]
    assert len(f) == 1 and f[0]["accFillSz"] == "10"


def test_legacy_registry_without_child_kept_while_pending(base, monkeypatch):
    cfg, tmp = base
    old = (settings.now() - dt.timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S%z")
    reg = {"triggers": {}, "positions": {"BTC-USDT-SWAP": {"opened": old}}, "watch": {"last_algo_uts": 1}}
    w = _watch(monkeypatch, tmp, reg, "live", pending=[CHILD])
    w.run_once(cfg)
    assert "BTC-USDT-SWAP" in reg["positions"]
