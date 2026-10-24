"""run_daily.sh 加固：解释器缺失 → 返回 5；窗口判断调用失败 → 返回 6；两者都经 webhook 通道告警（本地捕获，不发真实 webhook）。
正常“不在窗口”仍返回 0 且不告警。"""
import json, os, subprocess, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def hook():
    got = []
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200); self.end_headers()
        def log_message(self, *a): pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/hook", got
    srv.shutdown()


def _env(tmp, url, py, now="08:00"):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OKX_", "TA_NOTIFY"))}
    stub = tmp / "stub.sh"; stub.write_text(f"touch {tmp}/entered.txt\n")
    env.update(TA_DESK_HOME=str(tmp), TA_DAILY_RUNNER=f"bash {stub}", TA_EVENT_SOURCE="test",
               TA_DAILY_NOW=now, TA_NOTIFY_WEBHOOK_URL=url, TA_DESK_PYTHON=str(py))
    return env


def _run(env, *args):
    return subprocess.run(["bash", str(ROOT / "run_daily.sh"), "--date", "2030-03-04", *args], env=env,
                          capture_output=True, text=True, timeout=60)


def test_missing_interpreter_exit5_and_alert(tmp_path, hook):
    url, got = hook
    r = _run(_env(tmp_path, url, tmp_path / "no-such-python"), "--session", "evening")
    assert r.returncode == 5
    assert "解释器缺失" in r.stderr
    assert not (tmp_path / "entered.txt").exists()
    assert len(got) == 1 and got[0]["event_type"] == "daily_skipped"
    assert "20:30晚场" in got[0]["message"] and "解释器缺失" in got[0]["message"]
    assert "解释器缺失" in (tmp_path / "logs").joinpath(next(p.name for p in (tmp_path / "logs").iterdir())).read_text()


def test_window_check_failure_exit6_and_alert(tmp_path, hook):
    url, got = hook
    fake = tmp_path / "fakepy"
    fake.write_text(f'#!/usr/bin/env bash\n[ "$2" = "desk.sessions" ] && {{ echo boom >&2; exit 1; }}\nexec {sys.executable} "$@"\n')
    fake.chmod(0o755)
    r = _run(_env(tmp_path, url, fake))
    assert r.returncode == 6
    assert not (tmp_path / "entered.txt").exists()
    assert len(got) == 1 and "窗口判断失败" in got[0]["message"] and "8点早场" in got[0]["message"]


def test_out_of_window_still_exit0_no_alert(tmp_path, hook):
    url, got = hook
    r = _run(_env(tmp_path, url, sys.executable, now="23:00"))
    assert r.returncode == 0 and "不在" in r.stdout
    assert not (tmp_path / "entered.txt").exists() and got == []


def test_channel_unavailable_logs_and_stderr(tmp_path):
    env = _env(tmp_path, "", tmp_path / "no-such-python")
    r = _run(env)
    assert r.returncode == 5 and "webhook 通道不可用" in r.stderr


def test_sessions_window_error_returns_3():
    from desk import sessions
    assert sessions.main(["window", "morning", "not-a-date", "08:00"]) == 3
