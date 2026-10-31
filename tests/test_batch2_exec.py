"""第二批：R5 翻页、R8 每币执行锁、D1 第 1–3 点执行前复核。"""
import multiprocessing as mp
import time

import pytest

from desk import settings


def test_r5_positions_history_paginates(monkeypatch):
    from desk import demo_exec as DX, okx_demo as X
    now = int(time.time() * 1000)
    pages = {None: [{"uTime": str(now - i), "realizedPnl": "-1"} for i in range(100)],
             str(now - 99): [{"uTime": str(now - 100 - i), "realizedPnl": "-2"} for i in range(30)]}
    seen = []

    def fake_get(path, **kw):
        seen.append(kw.get("after"))
        return {"data": pages[kw.get("after")]}
    monkeypatch.setattr(X, "get", fake_get)
    rows = DX.positions_history()
    assert len(rows) == 130 and seen == [None, str(now - 99)]
    assert DX.today_realized() == pytest.approx(-100 - 60)


def test_r5_pagination_failure_rejects(monkeypatch):
    from desk import demo_exec as DX, okx_demo as X
    monkeypatch.setattr(X, "get", lambda path, **kw: {"data": [{"uTime": str(int(time.time() * 1000)), "realizedPnl": "0"}] * 100})
    with pytest.raises(X.DemoError):
        DX.positions_history(max_pages=3)


def _worker(q):
    from desk import exec_lock
    exec_lock._held.clear()   # fork 继承了父进程的可重入计数，子进程是独立持锁方
    try:
        with exec_lock.hold("BTC-USDT-SWAP", timeout=0.5):
            q.put("got")
            time.sleep(1.5)
    except exec_lock.ExecLockTimeout:
        q.put("timeout")


def test_r8_exec_lock_mutual_exclusion():
    from desk import exec_lock
    q = mp.get_context("fork").Queue()
    with exec_lock.hold("BTC-USDT-SWAP", timeout=1):
        p = mp.get_context("fork").Process(target=_worker, args=(q,))
        p.start(); p.join(5)
        assert q.get(timeout=1) == "timeout"
        with exec_lock.hold("BTC-USDT-SWAP"):   # 同进程可重入
            pass
    ev = [e for e in settings.read_jsonl(__import__("desk.okx_demo", fromlist=["x"]).EVENTS) if e.get("event") == "exec_lock_timeout"]
    assert ev  # 子进程记了事件（写在同一 tmp HOME）
    with exec_lock.hold("ETH-USDT-SWAP", timeout=0.5):   # 不同币互不影响
        pass


def test_r8_second_open_sees_position(monkeypatch):
    """锁内重读账户：第二次执行看到已有持仓 → 不开仓。"""
    from desk import demo_exec as DX, manager
    from tests._tool_tm import from_side
    cfg = settings.load_config()
    held = {"pos": []}
    bk = lambda cfg, prices=None: {"equity": 1e5, "live_equity": 1e5, "positions": list(held["pos"]), "triggers": [], "pending_entries": []}
    monkeypatch.setattr(DX, "book", bk)
    opened = []

    def fake_open(*a, **k):
        opened.append(1)
        held["pos"].append({"inst_id": "BTC-USDT-SWAP", "side": "long", "venue": "okx_demo", "notional": 1, "price": 1})
        return True, {}
    monkeypatch.setattr(DX, "open_now", fake_open)
    from desk import okx_demo as X
    monkeypatch.setattr(X, "contracts", lambda inst, q: "1")
    monkeypatch.setattr(DX, "cancel_triggers", lambda *a, **k: [])
    st = {r: {"direction": "bullish", "confidence": 0.8, "entry_px": 100.0, "entry_condition": "now", "horizon_days": 3,
              "stop": 95.0, "target": 110.0} for r in manager.VOTERS}
    for _ in range(2):
        manager.pm_execute_demo(cfg, cfg["coins"][0], "D-R8", st, manager.weights(), {"last": 100.0}, None, None, "t",
                                tm=from_side("long", entry=100.0, sl=95.0, tp=110.0), tm_state="go")
    assert opened == [1]


def test_d1_gates(monkeypatch):
    from desk import manager, okx_data
    cfg = settings.load_config()
    import datetime as d
    today = d.datetime.now(d.timezone.utc).date()
    mk = {"last": 100.0, "atr14": 2.0, "bar_date": str(today - d.timedelta(days=1))}
    monkeypatch.setattr(okx_data, "last_price", lambda inst: 101.2)   # 0.6×ATR
    assert manager.pre_trade_gate(cfg, "BTC-USDT-SWAP", mk) == "price_drift_skip"
    monkeypatch.setattr(okx_data, "last_price", lambda inst: 100.5)
    assert manager.pre_trade_gate(cfg, "BTC-USDT-SWAP", mk) is None
    assert manager.pre_trade_gate(cfg, "BTC-USDT-SWAP", {**mk, "bar_date": str(today - d.timedelta(days=5))}) == "data_stale_no_trade"
    okx_data._FALLBACK.add("BTC-USDT-SWAP")
    try:
        assert manager.pre_trade_gate(cfg, "BTC-USDT-SWAP", mk) == "data_fallback_no_trade"
    finally:
        okx_data._FALLBACK.discard("BTC-USDT-SWAP")


def test_d1_okx_failure_marks_fallback(monkeypatch):
    from desk import okx_data
    monkeypatch.setattr(okx_data, "candles", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setitem(okx_data._orig, "load_ohlcv", lambda *a, **k: "yahoo")
    try:
        assert okx_data.okx_load_ohlcv("BTC-USD", "2026-10-22") == "yahoo"
        assert okx_data.fallback_used("BTC-USDT-SWAP")
    finally:
        okx_data._FALLBACK.clear()
