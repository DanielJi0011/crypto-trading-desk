"""第二批剩余：R4 check_open 拒单单测、R6 补单完整风控、R12 持有期从首次成交起算、执行锁覆盖平仓/减仓/补单。离线。"""
import contextlib
import datetime as dt

import pytest

from desk import demo_exec as DX, entry_limit as EL, okx_demo as X, settings, equity_guard as EG, exec_lock

INSTR = {"ctVal": "0.01", "lotSz": "0.01", "minSz": "0.01", "tickSz": "0.1"}
_REAL_FULL = EL._full_check


@pytest.fixture
def base(monkeypatch, tmp_path):
    monkeypatch.setattr(X, "_inst_cache", {"BTC-USDT-SWAP": INSTR, "ETH-USDT-SWAP": INSTR, "SOL-USDT-SWAP": INSTR})
    monkeypatch.setattr(X, "EVENTS", tmp_path / "ev.jsonl")
    monkeypatch.setattr(EG, "MARKS", tmp_path / "m.json")
    monkeypatch.setattr(DX, "today_realized", lambda: 0.0); monkeypatch.setattr(DX, "today_orders", lambda: 0)
    return settings.load_config()


def _bk(positions):
    return {"equity": 100000, "live_equity": 100000, "positions": positions, "triggers": [], "pending_entries": [],
            "exposure": sum(p["notional"] for p in positions)}


def _pos(inst, side, notional, px, sl):
    return {"inst_id": inst, "side": side, "notional": notional, "price": px, "avg_px": px, "sl_px": sl}


def test_net_directional_rejects_same_side_only(base):
    bk = _bk([_pos("ETH-USDT-SWAP", "long", 110000, 2000, 1990)])
    r, ch = DX.check_open(base, bk, "BTC-USDT-SWAP", 0.15, 100000, 99000, "long", 1)
    assert any("[NET_DIRECTIONAL]" in x for x in r) and ch["net_directional_after"] == pytest.approx(125000)
    r2, _ = DX.check_open(base, bk, "BTC-USDT-SWAP", 0.15, 100000, 101000, "short", 1)
    assert not any("[NET_DIRECTIONAL]" in x for x in r2)


def test_open_risk_rejects(base):
    bk = _bk([_pos("ETH-USDT-SWAP", "short", 30000, 2000, None)])   # 无止损按 8% 估：2400 > 2000
    r, ch = DX.check_open(base, bk, "BTC-USDT-SWAP", 0.01, 100000, 99000, "long", 1)
    assert any("[OPEN_RISK]" in x for x in r) and ch["open_risk_after"] > 2000
    ok, _ = DX.check_open(base, _bk([_pos("ETH-USDT-SWAP", "short", 30000, 2000, 2010)]), "BTC-USDT-SWAP", 0.01, 100000, 99000, "long", 1)
    assert not any("[OPEN_RISK]" in x for x in ok)


def _entry():
    return {"inst": "BTC-USDT-SWAP", "side": "long", "pos_side": "long", "stop_loss": 99000, "take_profit": 103000,
            "lever": 1, "accFillSz": "5", "remain": "10"}


def _ticker(monkeypatch):
    monkeypatch.setattr(X, "get", lambda path, **kw: {"data": [{"last": "100000"}]} if path == "/api/v5/market/ticker"
                        else (_ for _ in ()).throw(AssertionError(path)))


def test_fallback_full_check_blocks_net(base, monkeypatch):
    _ticker(monkeypatch)
    bk = _bk([_pos("ETH-USDT-SWAP", "long", 110000, 2000, 1990), _pos("BTC-USDT-SWAP", "long", 5000, 100000, 99000)])
    monkeypatch.setattr(DX, "book", lambda cfg, prices=None: bk)
    r = _REAL_FULL(base, _entry())   # 总 15 张 = 0.15 BTC = 15000 名义；本币已成交部分不重复计
    assert any("[NET_DIRECTIONAL]" in x for x in r) and not any("[ONE_PER_COIN]" in x for x in r)


def test_fallback_full_check_passes_clean(base, monkeypatch):
    _ticker(monkeypatch)
    monkeypatch.setattr(DX, "book", lambda cfg, prices=None: _bk([_pos("BTC-USDT-SWAP", "long", 5000, 100000, 99000)]))
    assert _REAL_FULL(base, _entry()) == []


def test_fallback_full_check_fail_closed(base, monkeypatch):
    def boom(cfg, prices=None):
        raise X.DemoError("down")
    monkeypatch.setattr(DX, "book", boom)
    assert "[GUARD_DATA]" in _REAL_FULL(base, _entry())[0]


def test_deadline_from_first_fill(base, monkeypatch):
    from desk import demo_watch
    ft = int(dt.datetime(2026, 10, 23, 10, 0, tzinfo=settings.now().tzinfo).timestamp() * 1000)
    monkeypatch.setattr(X, "get", lambda path, **kw: {"data": [{"state": "partially_filled", "accFillSz": "1", "fillTime": str(ft)}]})
    m = {"child_ordId": "C1", "horizon_days": 2, "deadline": "2026-10-24 09:00:00+0800"}
    demo_watch._entry_filled("BTC-USDT-SWAP", m)
    assert m["deadline"] == "2026-10-25 10:00:00+0800" and m["deadline_from_fill"]
    m2 = dict(m); demo_watch._entry_filled("BTC-USDT-SWAP", m2)
    assert m2["deadline"] == m["deadline"]   # 只按首次成交设一次


@pytest.fixture
def locks(monkeypatch):
    seen = []

    @contextlib.contextmanager
    def hold(inst, timeout=120.0, why=""):
        seen.append((inst, why)); yield
    monkeypatch.setattr(exec_lock, "hold", hold)
    return seen


def test_close_and_reduce_take_lock(base, monkeypatch, locks):
    monkeypatch.setattr(DX, "_require", lambda: None)
    monkeypatch.setattr(X, "post", lambda path, body, **k: {"data": [{"algoId": "N"}]})
    monkeypatch.setattr(DX, "reg", lambda: {"positions": {}})
    monkeypatch.setattr(DX, "save_reg", lambda r: None)
    monkeypatch.setattr(DX, "_replace_close_algos", lambda *a: "N")
    monkeypatch.setattr(X, "instrument", lambda inst: INSTR)
    p = {"inst_id": "ETH-USDT-SWAP", "pos_side": "short", "side": "short", "trade_id": "t", "price": 1, "upnl": 0,
         "contracts": 10, "sl_px": 2, "tp_px": 0.5}
    DX.close(base, p, "x"); DX.reduce(base, p, 0.5, "y")
    assert locks == [("ETH-USDT-SWAP", "close"), ("ETH-USDT-SWAP", "reduce")]


def test_entry_limit_step_takes_lock(base, monkeypatch, tmp_path, locks):
    monkeypatch.setattr(EL, "PATH", tmp_path / "el.json"); monkeypatch.setattr(EL, "LOCK", tmp_path / "el.lock")
    EL.register("A9", inst="SOL-USDT-SWAP", side="short", pos_side="short", sz="1", order_px="100", trigger_px=100,
                stop_loss=105, take_profit=90, attach_cl="c", lever=1, decision_id="d")
    monkeypatch.setattr(EL, "_step", lambda *a: "armed")
    assert EL.check_timeouts(base, now_ms=1) == [("A9", "armed")]
    assert locks == [("SOL-USDT-SWAP", "entry_limit")]
