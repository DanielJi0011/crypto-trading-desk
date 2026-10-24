"""O1 时间窗口、daily_done；O2 链路告警。全部用临时目录与桩通知，不发 webhook，不调用 LLM。"""
import datetime as dt
import json
import os
import subprocess
from pathlib import Path

import pytest

from desk import chain_health, settings

ROOT = Path(__file__).resolve().parents[1]
TZ = dt.timezone(dt.timedelta(hours=8))


def _env(tmp, now):
    s = tmp / "stub.sh"; s.write_text(f"echo run >> {tmp}/entered.txt\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OKX_", "TA_NOTIFY_WEBHOOK"))}
    env.update(TA_DESK_HOME=str(tmp), TA_DAILY_RUNNER=f"bash {s}", TA_NOTIFY_DISABLED="1", TA_EVENT_SOURCE="test", TA_DAILY_NOW=now)
    return env


def _ev(tmp):
    p = tmp / "state" / "ledger" / "demo_events.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


@pytest.mark.parametrize("now,enter", [("07:54", False), ("07:55", True), ("12:00", True), ("12:01", False), ("23:00", False)])
def test_window(tmp_path, now, enter):
    r = subprocess.run(["bash", str(ROOT / "run_daily.sh"), "--date", "2030-02-01"], env=_env(tmp_path, now),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0
    assert (tmp_path / "entered.txt").exists() == enter
    kinds = [(e["event"], e.get("why")) for e in _ev(tmp_path)]
    assert (("daily_done", None) in kinds) == enter
    assert (("daily_duplicate_trigger", "out_of_window") in kinds) == (not enter)
    assert bool(list((tmp_path / "backups").glob("state-*.tar.gz"))) == enter      # 成功后自动备份


def test_window_override(tmp_path):
    env = _env(tmp_path, "23:00"); env["TA_DAILY_IGNORE_WINDOW"] = "1"
    subprocess.run(["bash", str(ROOT / "run_daily.sh"), "--date", "2030-02-02"], env=env, capture_output=True, timeout=60)
    assert (tmp_path / "entered.txt").exists()


@pytest.fixture
def iso(tmp_path, monkeypatch):
    from desk import okx_demo as X, stop_coverage
    st = tmp_path / "state"; st.mkdir()
    monkeypatch.setattr(settings, "STATE", st)
    monkeypatch.setattr(settings, "KILL_PATH", st / "KILL")
    monkeypatch.setattr(settings, "LEGACY_KILL_PATH", st / "ledger" / "KILL")
    monkeypatch.setattr(settings, "HALT_PATH", st / "HALT")
    monkeypatch.setattr(chain_health, "PATH", st / "chain_health.json")
    monkeypatch.setattr(chain_health, "HC_STATE", st / "healthcheck_state.json")
    monkeypatch.setattr(stop_coverage, "PATH", st / "stop_coverage.json")
    monkeypatch.setattr(X, "EVENTS", tmp_path / "ev.jsonl")
    monkeypatch.setattr(X, "INTENTS", tmp_path / "intents.jsonl")
    (st / "demo_ws_heartbeat").write_text("x")
    monkeypatch.setattr(chain_health, "running", lambda: set(chain_health.PROCS))
    return st


def test_process_missing_alert_only(iso, monkeypatch):
    monkeypatch.setattr(chain_health, "running", lambda: {"demo_ws"})
    got = []
    sent = chain_health.run({}, dt.datetime(2030, 1, 1, 8, 0, tzinfo=TZ), notifier=got.append)
    assert "chain_unhealthy:proc_demo_watch" in sent and "chain_unhealthy:proc_demo_ws" not in sent
    assert any("demo_watch" in m["message"] for m in got)


def test_running_scans_proc():
    assert isinstance(chain_health.running(), set)


def test_flags_daily_missing_dedup_heartbeat(iso):
    cfg = settings.load_config()
    sent = []
    n = lambda ev: sent.append(ev["kind"])
    t0 = dt.datetime(2030, 3, 1, 9, 0, tzinfo=TZ)
    hb = (t0 + dt.timedelta(minutes=70)).timestamp()
    os.utime(iso / "demo_ws_heartbeat", (hb, hb))         # 心跳新鲜，排除 chain_unhealthy 干扰
    chain_health.run(cfg, t0, n)
    assert sent == []                                     # 首次只记录开关状态；09:30 前不报缺失
    (iso / "KILL").write_text("x")
    chain_health.run(cfg, t0 + dt.timedelta(minutes=40), n)
    assert sent == ["kill_on", "daily_missing"]
    chain_health.run(cfg, t0 + dt.timedelta(minutes=50), n)
    assert sent == ["kill_on", "daily_missing"]           # 每天一次
    (iso / "KILL").unlink(); (iso / "HALT").write_text("x")
    chain_health.run(cfg, t0 + dt.timedelta(minutes=60), n)
    assert sent[-2:] == ["kill_off", "halt_on"]
    assert "heartbeat" not in sent                        # 默认关闭
    cfg["alerts"]["heartbeat_hours"] = 1
    sent.clear(); chain_health.run(cfg, t0 + dt.timedelta(minutes=70), n)
    assert sent == ["heartbeat"]


def test_chain_unhealthy_dedup(iso):
    cfg = settings.load_config()
    (iso / "stop_coverage.json").write_text(json.dumps({"missing": {"BTC-USDT-SWAP:net": 2}}))
    os.utime(iso / "demo_ws_heartbeat", (0, 0))
    sent = []
    t0 = dt.datetime(2030, 3, 1, 8, 0, tzinfo=TZ)
    chain_health.run(cfg, t0, lambda ev: sent.append((ev["kind"], ev["message"])))
    assert [k for k, _ in sent] == ["chain_unhealthy", "chain_unhealthy"]
    assert any("止损覆盖" in m for _, m in sent) and any("demo_ws" in m for _, m in sent)
    chain_health.run(cfg, t0 + dt.timedelta(minutes=10), lambda ev: sent.append((ev["kind"], ev["message"])))
    assert len(sent) == 2                                 # 同一问题 1 小时内不重复
    chain_health.run(cfg, t0 + dt.timedelta(minutes=61), lambda ev: sent.append((ev["kind"], ev["message"])))
    assert len(sent) == 4


def test_healthcheck_stale(iso):
    sent = []
    (iso / "healthcheck_state.json").write_text(json.dumps({"last_run": 1000.0}))
    assert chain_health.healthcheck_stale(1000 + 600, lambda e: sent.append(e["kind"])) is False
    assert chain_health.healthcheck_stale(1000 + 1801, lambda e: sent.append(e["kind"])) is True
    assert chain_health.healthcheck_stale(1000 + 2400, lambda e: sent.append(e["kind"])) is False
    assert sent == ["healthcheck_stale"]
    from desk import demo_ws
    for k in ["daily_missing", "daily_done", "healthcheck_stale", "kill_on", "kill_off", "halt_on", "halt_off", "chain_unhealthy", "heartbeat"]:
        assert k in demo_ws.NOTIFY_KINDS


def test_backup_excludes_secrets(tmp_path):
    st = tmp_path / "state"; st.mkdir()
    (st / "secrets.env").write_text("X=1"); (st / "a.json").write_text("{}")
    env = {k: v for k, v in os.environ.items() if not k.startswith("OKX_")}
    env["TA_DESK_HOME"] = str(tmp_path)
    subprocess.run(["bash", str(ROOT / "scripts/backup_state.sh")], env=env, check=True, capture_output=True, timeout=60)
    import tarfile
    f = next((tmp_path / "backups").glob("state-*.tar.gz"))
    names = tarfile.open(f).getnames()
    assert "state/a.json" in names and not any("secrets" in n for n in names)


def test_kill_drill_tag(iso):
    got = []
    t0 = dt.datetime(2030, 3, 1, 8, 0, tzinfo=TZ)
    chain_health.run({}, t0, got.append)
    (iso / "KILL").write_text("演练 drill")
    chain_health.run({}, t0, got.append)
    (iso / "KILL").unlink()
    chain_health.run({}, t0, got.append)
    (iso / "KILL").write_text("真实停机")
    chain_health.run({}, t0, got.append)
    m = [(e["kind"], e["message"]) for e in got if e["kind"].startswith("kill")]
    assert m[0][0] == "kill_on" and m[0][1].startswith("【演练】")
    assert m[1][0] == "kill_off" and m[1][1].startswith("【演练】")
    assert m[2][0] == "kill_on" and "演练" not in m[2][1]
