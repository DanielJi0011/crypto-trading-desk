"""R6b：demo_ws 定时检查限价入场超时。离线。"""
from desk import demo_ws as WS, entry_limit as EL, settings


def test_secs_from_config():
    cfg = settings.load_config()
    assert WS.entry_timer_secs(cfg) == 20
    cfg["execution"]["entry_limit"]["ws_check_secs"] = 0
    assert WS.entry_timer_secs(cfg) == 0
    cfg["execution"]["entry_limit"].update(ws_check_secs=20, enabled=False)
    assert WS.entry_timer_secs(cfg) == 0


def test_tick_skips_without_open_entries(monkeypatch):
    called = []
    monkeypatch.setattr(EL, "check_timeouts", lambda cfg: called.append(1) or [])
    monkeypatch.setattr(EL, "_load", lambda: {"entries": {"a": {"status": "done"}}})
    assert WS.entry_limit_tick({}) == [] and not called


def test_tick_runs_with_open_entry(monkeypatch):
    monkeypatch.setattr(EL, "check_timeouts", lambda cfg: [("a", "fallback_filled")])
    monkeypatch.setattr(EL, "_load", lambda: {"entries": {"a": {"status": "armed", "triggered_ms": 1}}})
    assert WS.entry_limit_tick({}) == [("a", "fallback_filled")]


def test_tick_halt_no_call(monkeypatch):
    monkeypatch.setattr(settings, "halt_active", lambda: True)
    monkeypatch.setattr(EL, "check_timeouts", lambda cfg: (_ for _ in ()).throw(AssertionError("不应调用")))
    assert WS.entry_limit_tick({}) == []
