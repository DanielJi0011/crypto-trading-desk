"""2026-11-01 静默运维告警：stale_code 与心跳超时类只写日志/quiet_alerts.jsonl，不发 webhook；其它类别仍通知（离线）。"""
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from desk import chain_health, demo_ws as W, settings

TZ = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def iso(tmp_path, monkeypatch):
    from desk import okx_demo as X, stop_coverage
    st = tmp_path / "state"; (st / "ledger").mkdir(parents=True)
    monkeypatch.setattr(settings, "STATE", st)
    monkeypatch.setattr(settings, "KILL_PATH", st / "KILL")
    monkeypatch.setattr(settings, "LEGACY_KILL_PATH", st / "ledger" / "KILL")
    monkeypatch.setattr(settings, "HALT_PATH", st / "HALT")
    monkeypatch.setattr(chain_health, "PATH", st / "chain_health.json")
    monkeypatch.setattr(chain_health, "HC_STATE", st / "healthcheck_state.json")
    monkeypatch.setattr(stop_coverage, "PATH", st / "stop_coverage.json")
    monkeypatch.setattr(X, "EVENTS", tmp_path / "ev.jsonl")
    monkeypatch.setattr(X, "INTENTS", tmp_path / "intents.jsonl")
    monkeypatch.setattr(W, "QUIET_LOG", st / "alerts" / "quiet_alerts.jsonl")
    monkeypatch.setattr(W, "quiet_ops_alerts", lambda: True)
    (st / "demo_ws_heartbeat").write_text("x")
    monkeypatch.setattr(chain_health, "running", lambda: set(chain_health.PROCS))
    return st


def _quiet(st):
    p = st / "alerts" / "quiet_alerts.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_default_switch_on_and_config():
    assert W._notify_section().get("quiet_ops_alerts") is True
    assert W.quiet_ops_alerts() is True


def test_switch_parsing(monkeypatch):
    for v, exp in ((None, True), (True, True), (False, False), ("false", False), ("off", False), (0, False)):
        monkeypatch.setattr(W, "_notify_section", lambda v=v: {} if v is None else {"quiet_ops_alerts": v})
        assert W.quiet_ops_alerts() is exp


def test_stale_code_quiet_logs_not_notify(iso, monkeypatch):
    monkeypatch.setattr(W, "_code_rev", {"start": "a" * 40, "alerted": 0.0})
    monkeypatch.setattr(W, "run_branch", lambda: "chore/publish-prep")
    sent = []
    assert W.check_code_version(now=1e9, head="b" * 40, notifier=sent.append, branch="chore/publish-prep")
    assert sent == []
    q = _quiet(iso)
    assert [r["kind"] for r in q] == ["stale_code"] and q[0]["notified"] is False and "旧代码" in q[0]["message"]


def test_notify_guard_blocks_quiet_kinds_but_not_trades(iso, monkeypatch):
    import requests
    posts = []

    class R:
        ok, status_code = True, 200
    monkeypatch.setattr(requests, "post", lambda *a, **k: posts.append(k["data"]) or R())
    monkeypatch.delenv("TA_NOTIFY_DISABLED", raising=False)
    monkeypatch.setattr(W, "_notify_cfg", lambda: ("http://example.invalid/hook", ""))
    monkeypatch.setattr(W, "COOLDOWN_PATH", iso / "cd.json")
    assert not W.notify({"kind": "stale_code", "message": "x"})
    assert not W.notify({"kind": "healthcheck_stale", "message": "y"})
    assert posts == []
    for k in ("fill", "stop_loss", "position_open", "trigger_placed", "kill_on", "halt_on", "daily_loss_halt",
              "drawdown_kill", "stop_coverage_missing"):
        assert W.notify({"kind": k, "message": k, "inst_id": "BTC-USDT-SWAP"})
    assert len(posts) == 9
    assert {r["kind"] for r in _quiet(iso)} == {"stale_code", "healthcheck_stale"}


def test_healthcheck_stale_quiet(iso):
    sent = []
    (iso / "healthcheck_state.json").write_text(json.dumps({"last_run": 1000.0}))
    assert chain_health.healthcheck_stale(1000 + 3601, sent.append) is True
    assert sent == [] and [r["kind"] for r in _quiet(iso)] == ["healthcheck_stale"]


def test_heartbeat_stale_proc_alive_quiet_others_notify(iso):
    os.utime(iso / "demo_ws_heartbeat", (0, 0))
    (iso / "stop_coverage.json").write_text(json.dumps({"missing": {"BTC-USDT-SWAP:net": 2}}))
    sent = []
    r = chain_health.run({}, dt.datetime(2030, 3, 1, 8, 0, tzinfo=TZ), notifier=sent.append)
    assert "quiet:demo_ws" in r and "chain_unhealthy:stop_coverage" in r
    assert all("心跳" not in e["message"] for e in sent) and any("止损覆盖" in e["message"] for e in sent)
    q = _quiet(iso)
    assert q and q[0]["kind"] == "chain_unhealthy" and q[0]["problem"] == "demo_ws"
    assert "demo_ws" in json.loads((iso / "chain_health.json").read_text())["quiet"]


def test_heartbeat_stale_proc_dead_still_notifies(iso, monkeypatch):
    os.utime(iso / "demo_ws_heartbeat", (0, 0))
    monkeypatch.setattr(chain_health, "running", lambda: {"demo_watch"})
    sent = []
    r = chain_health.run({}, dt.datetime(2030, 3, 1, 8, 0, tzinfo=TZ), notifier=sent.append)
    assert "chain_unhealthy:demo_ws" in r and "chain_unhealthy:proc_demo_ws" in r
    assert any("心跳" in e["message"] for e in sent) and any("不在运行" in e["message"] for e in sent)
    assert _quiet(iso) == []


def test_kill_halt_still_notify(iso):
    sent = []
    t0 = dt.datetime(2030, 3, 1, 8, 0, tzinfo=TZ)
    chain_health.run({}, t0, lambda e: sent.append(e["kind"]))
    (iso / "KILL").write_text("x")
    chain_health.run({}, t0 + dt.timedelta(minutes=10), lambda e: sent.append(e["kind"]))
    assert "kill_on" in sent


def _hc(monkeypatch, tmp_path, alive_after=True, stale_name="demo_ws"):
    spec = importlib.util.spec_from_file_location("hc_quiet", ROOT / "scripts" / "healthcheck.py")
    hc = importlib.util.module_from_spec(spec); spec.loader.exec_module(hc)
    monkeypatch.chdir(tmp_path)   # main() 会 os.chdir(ROOT)，用例结束恢复 cwd
    monkeypatch.setattr(hc, "ROOT", tmp_path); monkeypatch.setattr(hc, "LOG", tmp_path / "hc.log")
    monkeypatch.setattr(hc, "STATE", tmp_path / "hc.json")
    (tmp_path / ".venv" / "bin").mkdir(parents=True); (tmp_path / ".venv" / "bin" / "python").write_text("")
    (tmp_path / "hc.json").write_text(json.dumps({"last_run": __import__("time").time() - 60}))
    monkeypatch.setattr(hc, "pids", lambda pat: [1])
    monkeypatch.setattr(hc, "stop", lambda n: None); monkeypatch.setattr(hc, "start", lambda n: None)
    monkeypatch.setattr(hc, "age", lambda p: 7200 if stale_name == "demo_ws" else 5)
    monkeypatch.setattr(hc, "watch_log_age", lambda: 7200 if stale_name == "demo_watch" else 5)
    monkeypatch.setattr(hc.time, "sleep", lambda s: None)
    monkeypatch.setattr(chain_health, "run", lambda cfg: [])
    got = []
    monkeypatch.setattr(hc, "notify", lambda st, issue, fixes: got.append(issue))
    return hc, got


@pytest.mark.parametrize("name", ["demo_ws", "demo_watch"])
def test_healthcheck_stale_restart_quiet(iso, monkeypatch, tmp_path, name):
    hc, got = _hc(monkeypatch, tmp_path, stale_name=name)
    assert hc.main() == 0
    assert got == []
    assert "静默" in (tmp_path / "hc.log").read_text()
    assert [r["issue"] for r in _quiet(iso)] == [f"{name}_stale"]


def test_healthcheck_proc_down_still_notifies(iso, monkeypatch, tmp_path):
    hc, got = _hc(monkeypatch, tmp_path, stale_name="none")
    calls = {"n": 0}

    def pids(pat):   # 首轮检查 demo_watch 不在，重启后存活
        calls["n"] += 1
        return [] if (pat.startswith("bash run_demo_watch") and calls["n"] <= 1) else [1]
    monkeypatch.setattr(hc, "pids", pids)
    hc.main()
    assert got == ["demo_watch_down"] and _quiet(iso) == []


def test_healthcheck_quiet_off_restores_notify(iso, monkeypatch, tmp_path):
    monkeypatch.setattr(W, "quiet_ops_alerts", lambda: False)
    hc, got = _hc(monkeypatch, tmp_path, stale_name="demo_ws")
    hc.main()
    assert got == ["demo_ws_stale"]
