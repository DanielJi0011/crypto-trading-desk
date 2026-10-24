"""R5 第一批：当日亏损/回撤按实时权益（含浮亏），任一数据失败即拒绝开仓（fail-closed）。全部离线打桩。"""
import json

import pytest

from desk import settings, equity_guard as EG, demo_exec as DX, okx_demo as X


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(EG, "MARKS", tmp_path / "marks.json")
    monkeypatch.setattr(settings, "KILL_PATH", tmp_path / "KILL")
    monkeypatch.setattr(settings, "LEGACY_KILL_PATH", tmp_path / "ledger_KILL")
    monkeypatch.setattr(settings, "HALT_PATH", tmp_path / "HALT")
    monkeypatch.setattr(X, "EVENTS", tmp_path / "ev.jsonl")
    sent = []
    import desk.demo_ws as W
    monkeypatch.setattr(W, "notify", lambda ev: sent.append(ev["kind"]) or True)
    return tmp_path, sent


def _bk(live):
    return {"equity": 100000, "live_equity": live, "positions": [], "triggers": [], "exposure": 0, "pending_entries": []}


def _open(cfg, bk):
    return DX.check_open(cfg, bk, "BTC-USDT-SWAP", 0.1, 100000, 95000, "long", 1)[0]


def test_unrealized_loss_counts_and_rejects(env, monkeypatch):
    tmp, sent = env
    monkeypatch.setattr(DX, "today_realized", lambda: 0.0)          # 已实现 0
    monkeypatch.setattr(DX, "today_orders", lambda: 0)
    cfg = settings.load_config()
    EG.MARKS.write_text(json.dumps({"day": settings.today(), "day_start_equity": 100000, "peak_equity": 100000}))
    r = _open(cfg, _bk(97000))                                       # 浮亏 −3000 ≥ 2.5%
    assert any("[DAILY_LOSS]" in x for x in r)
    assert sent == ["daily_loss_halt"]
    _open(cfg, _bk(97000)); assert sent == ["daily_loss_halt"]      # 每日只通知一次
    assert not any("[DAILY_LOSS]" in x for x in _open(cfg, _bk(98000)))   # −2000 < 2.5%，不拒


def test_daily_halt_released_next_day(env):
    cfg = settings.load_config()
    EG.MARKS.write_text(json.dumps({"day": "2000-01-01", "day_start_equity": 100000, "peak_equity": 100000}))
    st = EG.evaluate(cfg, 97000.0)
    assert st["day_start"] == 97000.0 and not st["daily_halt"]


def test_balance_exception_rejects(env, monkeypatch):
    monkeypatch.setattr(DX, "today_realized", lambda: 0.0)
    monkeypatch.setattr(DX, "today_orders", lambda: 0)
    def boom():
        raise X.DemoError("balance down")
    monkeypatch.setattr(DX, "usdt_equity", boom)
    cfg = settings.load_config()
    r = _open(cfg, {**_bk(None)})
    assert any("[GUARD_DATA]" in x for x in r)
    assert any(json.loads(l)["event"] == "guard_data_unavailable" for l in (env[0] / "ev.jsonl").read_text().splitlines())


@pytest.mark.parametrize("bad", [0.0, float("nan"), -5.0])
def test_invalid_equity_rejects(env, monkeypatch, bad):
    monkeypatch.setattr(DX, "today_realized", lambda: 0.0)
    monkeypatch.setattr(DX, "today_orders", lambda: 0)
    assert any("[GUARD_DATA]" in x for x in _open(settings.load_config(), _bk(bad)))


def test_corrupt_marks_rejects(env, monkeypatch):
    monkeypatch.setattr(DX, "today_realized", lambda: 0.0)
    monkeypatch.setattr(DX, "today_orders", lambda: 0)
    EG.MARKS.write_text("{broken")
    assert any("[GUARD_DATA]" in x for x in _open(settings.load_config(), _bk(100000)))


def test_realized_pnl_exception_rejects(env, monkeypatch):
    def boom():
        raise X.DemoError("history down")
    monkeypatch.setattr(DX, "today_realized", boom)
    monkeypatch.setattr(DX, "today_orders", lambda: 0)
    assert any("[GUARD_DATA]" in x for x in _open(settings.load_config(), _bk(100000)))


def test_drawdown_writes_kill(env, monkeypatch):
    tmp, sent = env
    monkeypatch.setattr(DX, "today_realized", lambda: 0.0)
    monkeypatch.setattr(DX, "today_orders", lambda: 0)
    EG.MARKS.write_text(json.dumps({"day": settings.today(), "day_start_equity": 98500, "peak_equity": 110000}))
    r = _open(settings.load_config(), _bk(98000))                    # 回撤 10.9%
    assert settings.KILL_PATH.exists() and "drawdown_kill" in settings.KILL_PATH.read_text()
    assert "drawdown_kill" in sent and any("[KILL]" in x or "[DRAWDOWN]" in x for x in r)
    assert settings.kill_active()


def test_entry_limit_guard_fail_closed(env, monkeypatch):
    from desk import entry_limit as EL
    def boom():
        raise X.DemoError("balance down")
    monkeypatch.setattr(DX, "usdt_equity", boom)
    monkeypatch.setattr(DX, "today_realized", lambda: 0.0)
    assert any("[GUARD_DATA]" in x for x in EL._guard(settings.load_config()))
