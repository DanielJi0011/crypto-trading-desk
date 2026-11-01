"""ensure_running.sh：临时目录+假命令验证，不启动真实交易进程。"""
import os, subprocess, time, fcntl
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ensure_running.sh"


def _root(tmp_path, venv=True):
    r = tmp_path / "root"
    (r / "scripts").mkdir(parents=True)
    rec = tmp_path / "rec"
    rec.mkdir()
    body = '#!/usr/bin/env bash\nenv | grep -E "^TA_(NOTIFY_DISABLED|EVENT_SOURCE)=" > "%s/$(basename $0).env"; echo "$@" > "%s/$(basename $0).args"\n' % (rec, rec)
    for p in ("scripts/healthcheck_loop.sh", "run_demo_ws.sh", "run_demo_watch.sh"):
        (r / p).write_text(body)
    if venv:
        (r / ".venv/bin").mkdir(parents=True)
        py = r / ".venv/bin/python"; py.write_text("#!/bin/sh\n"); py.chmod(0o755)
    return r, rec


def _pgrep(tmp_path, running):
    f = tmp_path / "fake_pgrep"
    cases = " ".join(f"'{p}'" for p in running)
    f.write_text('#!/usr/bin/env bash\npat="$2"\nfor r in %s; do case "$pat" in *"$r"*) exit 0;; esac; done\nexit 1\n' % cases)
    f.chmod(0o755)
    return str(f)


def _run(root, pgrep, dry=True, extra=None):
    env = dict(os.environ, TA_ENSURE_ROOT=str(root), TA_ENSURE_PGREP=pgrep,
               TA_ENSURE_DRY_RUN="1" if dry else "0",
               TA_NOTIFY_DISABLED="1", TA_EVENT_SOURCE="test")
    env.update(extra or {})
    return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)


def test_all_running_no_action(tmp_path):
    r, _ = _root(tmp_path)
    p = _run(r, _pgrep(tmp_path, ["healthcheck_loop", "run_demo_ws", "run_demo_watch"]))
    assert p.returncode == 0 and "DRY" not in p.stdout


def test_missing_all_emits_commands_in_order(tmp_path):
    r, _ = _root(tmp_path)
    out = _run(r, _pgrep(tmp_path, [])).stdout.splitlines()
    cmds = [l for l in out if l.startswith("DRY env")]
    pre = "DRY env -u TA_NOTIFY_DISABLED -u TA_EVENT_SOURCE setsid nohup "
    assert cmds == [pre + "bash scripts/healthcheck_loop.sh", pre + "bash run_demo_ws.sh",
                    pre + "bash run_demo_watch.sh --loop 300"]


def test_only_missing_one_started(tmp_path):
    r, _ = _root(tmp_path)
    out = _run(r, _pgrep(tmp_path, ["healthcheck_loop", "run_demo_watch"])).stdout
    assert "run_demo_ws.sh" in out and "healthcheck_loop" not in out and "run_demo_watch" not in out


def test_venv_missing_takes_rebuild_branch(tmp_path):
    r, _ = _root(tmp_path, venv=False)
    out = _run(r, _pgrep(tmp_path, ["healthcheck_loop", "run_demo_ws", "run_demo_watch"])).stdout
    assert "DRY venv rebuild" in out


def test_venv_present_no_rebuild(tmp_path):
    r, _ = _root(tmp_path)
    assert "venv" not in _run(r, _pgrep(tmp_path, [])).stdout


def test_lock_prevents_concurrent(tmp_path):
    r, _ = _root(tmp_path)
    lock = tmp_path / "l.lock"
    with open(lock, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        p = _run(r, _pgrep(tmp_path, []), extra={"TA_ENSURE_LOCK": str(lock)})
    assert p.returncode == 0 and "DRY env" not in p.stdout and "SKIP locked" in p.stdout


def test_real_start_strips_test_env(tmp_path):
    r, rec = _root(tmp_path)
    assert _run(r, _pgrep(tmp_path, []), dry=False).returncode == 0
    names = ["healthcheck_loop.sh", "run_demo_ws.sh", "run_demo_watch.sh"]
    for _ in range(50):
        if all((rec / f"{n}.args").exists() for n in names):
            break
        time.sleep(0.1)
    for n in names:
        assert (rec / f"{n}.env").read_text().strip() == ""
    assert (rec / "run_demo_watch.sh.args").read_text().strip() == "--loop 300"


def test_script_never_touches_secrets():
    txt = SCRIPT.read_text()
    assert "secrets.env" not in txt.replace("不读取、不打印 state/secrets.env", "")
