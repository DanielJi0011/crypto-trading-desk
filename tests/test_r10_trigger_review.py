"""R10 第一批：KILL/HALT/日亏/回撤 → 撤入场类条件单与在途入场子单（含受保护单），绝不撤 reduceOnly 止损止盈。全部离线打桩。"""
import json

import pytest

from desk import settings, okx_demo as X, demo_exec as DX, equity_guard as EG, trigger_review as TR


class Ex:
    def __init__(self):
        self.algos = [
            {"algoId": "T1", "instId": "BTC-USDT-SWAP", "ordType": "trigger", "side": "buy", "posSide": "long", "reduceOnly": "false"},
            {"algoId": "PROT", "instId": "SOL-USDT-SWAP", "ordType": "trigger", "side": "sell", "posSide": "short", "reduceOnly": "false"},
            {"algoId": "TRO", "instId": "ETH-USDT-SWAP", "ordType": "trigger", "side": "buy", "posSide": "short", "reduceOnly": "true"},
        ]
        self.orders = [
            {"ordId": "C1", "instId": "BTC-USDT-SWAP", "side": "buy", "posSide": "long", "reduceOnly": "false", "sz": "2"},
            {"ordId": "R1", "instId": "ETH-USDT-SWAP", "side": "buy", "posSide": "short", "reduceOnly": "true", "sz": "1"},
            {"ordId": "CL", "instId": "ETH-USDT-SWAP", "side": "buy", "posSide": "short", "reduceOnly": "false", "sz": "1"},
        ]
        self.posts, self.gets = [], []

    def get(self, path, **kw):
        self.gets.append((path, kw))
        if path == "/api/v5/trade/orders-algo-pending":
            assert kw.get("ordType") == "trigger", "R10 不得查询/撤 conditional/oco 止损止盈"
            return {"data": list(self.algos)}
        if path == "/api/v5/trade/orders-pending":
            return {"data": list(self.orders)}
        if path == "/api/v5/trade/order":
            return {"data": [{"state": "canceled", "accFillSz": "0.4"}]}
        if path == "/api/v5/account/balance":
            return {"data": [{"details": [{"ccy": "USDT", "eq": "100000"}]}]}
        raise AssertionError(path)

    def post(self, path, body, allow_halt=False):
        self.posts.append((path, body, allow_halt)); return {"data": [{}]}


@pytest.fixture
def ex(tmp_path, monkeypatch):
    e = Ex()
    monkeypatch.setattr(X, "get", e.get); monkeypatch.setattr(X, "post", e.post)
    monkeypatch.setattr(X, "EVENTS", tmp_path / "ev.jsonl")
    monkeypatch.setattr(EG, "MARKS", tmp_path / "marks.json")
    monkeypatch.setattr(DX, "REG", tmp_path / "reg.json")
    for n in ("KILL", "HALT"):
        monkeypatch.setattr(settings, f"{n}_PATH", tmp_path / n)
    monkeypatch.setattr(settings, "LEGACY_KILL_PATH", tmp_path / "ledger_KILL")
    cfg = settings.load_config(); cfg["execution"]["protected_algo_ids"] = ["PROT"]
    e.sent = []
    e.notify = lambda ev: e.sent.append(ev["kind"])
    return e, cfg, tmp_path


def _cancelled(e):
    algos = [b[0]["algoId"] for p, b, _ in e.posts if p == "/api/v5/trade/cancel-algos"]
    orders = [b["ordId"] for p, b, _ in e.posts if p == "/api/v5/trade/cancel-order"]
    return algos, orders


def test_no_flag_no_cancel(ex):
    e, cfg, _ = ex
    assert TR.poll(cfg, notifier=e.notify) == {"reasons": []}
    assert e.posts == [] and not any(p.startswith("/api/v5/trade/orders") for p, _ in e.gets)


@pytest.mark.parametrize("flag", ["KILL", "ledger_KILL", "HALT"])
def test_kill_halt_revokes_entries_including_protected(ex, flag):
    e, cfg, tmp = ex
    (tmp / flag).write_text("x")
    r = TR.poll(cfg, notifier=e.notify)
    algos, orders = _cancelled(e)
    assert algos == ["T1", "PROT"]                 # 受保护单在停机时也撤
    assert orders == ["C1"]                        # 只撤开仓方向子单；reduceOnly 与平仓方向不撤
    assert all(a for _, _, a in e.posts)           # 撤单都经 allow_halt（HALT 下也能撤）
    assert e.sent == ["trigger_revoked"]
    ev = [json.loads(l) for l in (tmp / "ev.jsonl").read_text().splitlines()]
    assert any(x["event"] == "trigger_revoked" and x["algoId"] == "PROT" and x["protected"] for x in ev)
    assert any(x["event"] == "entry_child_revoked" and x["accFillSz"] == "0.4" for x in ev)


def test_daily_loss_revokes(ex):
    e, cfg, tmp = ex
    EG.MARKS.write_text(json.dumps({"day": settings.today(), "day_start_equity": 103000, "peak_equity": 103000}))
    r = TR.poll(cfg, notifier=e.notify)
    assert "daily_loss_halt" in r["reasons"] and _cancelled(e) == (["T1", "PROT"], ["C1"])


def test_drawdown_writes_kill_and_revokes(ex, monkeypatch):
    e, cfg, tmp = ex
    import desk.demo_ws as W
    monkeypatch.setattr(W, "notify", lambda ev: True)
    EG.MARKS.write_text(json.dumps({"day": settings.today(), "day_start_equity": 100000, "peak_equity": 112000}))
    r = TR.poll(cfg, notifier=e.notify)
    assert settings.KILL_PATH.exists() and "kill" in r["reasons"] and "drawdown_kill" in r["reasons"]
    assert _cancelled(e)[0] == ["T1", "PROT"]


def test_is_entry_rules():
    assert TR.is_entry({"side": "buy", "posSide": "long", "reduceOnly": "false"})
    assert not TR.is_entry({"side": "sell", "posSide": "long", "reduceOnly": "false"})
    assert not TR.is_entry({"side": "buy", "posSide": "net", "reduceOnly": "true"})
    assert TR.is_entry({"side": "sell", "posSide": "net", "reduceOnly": "false"})


def test_demo_ws_review_interval():
    import desk.demo_ws as W
    assert W.REVIEW_EVERY <= 30 and hasattr(W.Runner, "_risk_review")


def test_demo_ws_risk_review_calls_poll(monkeypatch, tmp_path):
    import asyncio
    import desk.demo_ws as W
    calls, slept = [], []
    monkeypatch.setattr(TR, "poll", lambda cfg: calls.append(1) or {"reasons": ["kill"], "algos": ["T1"], "orders": [], "errors": []})
    real_sleep = asyncio.sleep

    async def fake_sleep(s):
        slept.append(s)
        if len(slept) > 2:
            raise asyncio.CancelledError
        await real_sleep(0)
    monkeypatch.setattr(W.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(W, "State", lambda: None)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(W.Runner()._risk_review())
    assert slept[:2] == [30, 30] and len(calls) == 2
