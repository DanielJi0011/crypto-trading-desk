"""2c：run_daily.sh 单实例锁 + 完成标记 + 失败标记与 daily_failed 事件。交易流程用桩替代，绝不运行真正的 run_daily；不发 webhook。"""
import json
import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _env(tmp, stub):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OKX_", "TA_NOTIFY_WEBHOOK"))}
    env.update(TA_DESK_HOME=str(tmp), TA_DAILY_RUNNER=f"bash {stub}", TA_NOTIFY_DISABLED="1", TA_EVENT_SOURCE="test", TA_DAILY_NOW="08:00")
    return env


def _stub(tmp, rc=0, sleep=0):
    s = tmp / "stub.sh"
    s.write_text(f"echo run >> {tmp}/entered.txt\nsleep {sleep}\nexit {rc}\n")
    return s


def _events(tmp):
    p = tmp / "state" / "ledger" / "demo_events.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def test_two_triggers_only_one_enters(tmp_path):
    env = _env(tmp_path, _stub(tmp_path, sleep=3))
    a = subprocess.Popen(["bash", str(ROOT / "run_daily.sh"), "--date", "2030-01-02"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)
    b = subprocess.run(["bash", str(ROOT / "run_daily.sh"), "--date", "2030-01-02"], env=env, capture_output=True, text=True, timeout=60)
    assert b.returncode == 0 and "已有实例在运行" in b.stdout
    assert a.wait(timeout=60) == 0
    c = subprocess.run(["bash", str(ROOT / "run_daily.sh"), "--date", "2030-01-02"], env=env, capture_output=True, text=True, timeout=60)
    assert c.returncode == 0 and "已完成" in c.stdout
    assert (tmp_path / "entered.txt").read_text().count("run") == 1            # 只有一次进入交易流程
    assert (tmp_path / "state" / "daily_done" / "2030-01-02").exists()
    why = [e["why"] for e in _events(tmp_path) if e["event"] == "daily_duplicate_trigger"]
    assert why == ["running", "done"]


def test_failure_marks_and_event(tmp_path):
    env = _env(tmp_path, _stub(tmp_path, rc=3))
    r = subprocess.run(["bash", str(ROOT / "run_daily.sh"), "--date", "2030-01-03"], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 3
    assert (tmp_path / "state" / "daily_failed" / "2030-01-03").read_text().strip() == "3"
    assert not (tmp_path / "state" / "daily_done" / "2030-01-03").exists()
    ev = [e for e in _events(tmp_path) if e["event"] == "daily_failed"]
    assert len(ev) == 1 and ev[0]["rc"] == "3" and ev[0]["source"] == "test"


def test_daily_failed_calls_webhook_mechanism(monkeypatch, tmp_path):
    from desk import daily_gate, demo_ws, okx_demo as X
    monkeypatch.setattr(X, "EVENTS", tmp_path / "ev.jsonl")
    sent = []
    monkeypatch.setattr(demo_ws, "notify", lambda ev: sent.append(ev["kind"]) or True)
    assert daily_gate.main(["failed", "2030-01-04", "1"]) == 0
    assert sent == ["daily_failed"] and "daily_failed" in demo_ws.NOTIFY_KINDS


def test_mock_does_not_mark_done(tmp_path):
    env = _env(tmp_path, _stub(tmp_path))
    r = subprocess.run(["bash", str(ROOT / "run_daily.sh"), "--mock", "--date", "2030-01-05"], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and not (tmp_path / "state" / "daily_done" / "2030-01-05").exists()
