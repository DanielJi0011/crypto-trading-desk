"""纯脚本健康检查（不调用 LLM）：每 10 分钟由 scripts/healthcheck_loop.sh 调用（本机无 cron）。
- 检查 .venv/bin/python；ps 确认 demo_watch 循环 / demo_ws 进程；不在 → setsid nohup 按原方式重启；
- demo_ws 心跳、demo_watch 日志超过 30 分钟未更新且进程在 → 重启该进程；
- 只有发生重启/修复才 POST webhook（event_type=health_fix），同一问题 1 小时内最多通知一次；其余只写 logs/healthcheck.log。
- 整机暂停检测：距上次健康检查 > 15 分钟说明机器刚恢复，本轮跳过“新鲜度”判定（进程缺失仍会修复）。
!! 不读取/打印密钥（webhook 地址与 key 只经 demo_ws._notify_cfg 取用，不写日志）；不涉及任何下单。"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
LOG = ROOT / "logs" / "healthcheck.log"
STATE = ROOT / "state" / "healthcheck_state.json"
HEARTBEAT = ROOT / "state" / "demo_ws_heartbeat"
STALE = 1800
NOTIFY_COOLDOWN = 3600
PAUSE_GAP = 900

PROCS = {
    "demo_watch": {"pat": "bash run_demo_watch.sh --loop", "cmd": ["bash", "run_demo_watch.sh", "--loop", "300"]},
    "demo_ws": {"pat": "bash run_demo_ws.sh", "child": "python -m desk.demo_ws", "cmd": ["bash", "run_demo_ws.sh"]},
}


def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S%z")


def log(msg):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[healthcheck {now_str()}] {msg}\n")


def pids(pat):
    r = subprocess.run(["pgrep", "-f", pat], capture_output=True, text=True)
    return [int(x) for x in r.stdout.split() if int(x) != os.getpid()]


def start(name):
    subprocess.Popen(["setsid", "nohup", *PROCS[name]["cmd"]], cwd=ROOT, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def stop(name):
    p = PROCS[name]
    targets = pids(p["pat"]) + (pids(p["child"]) if p.get("child") else [])
    for pid in targets:   # 先停外层循环（否则它会立刻拉起子进程），再停子进程 / 进程组
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    for _ in range(20):
        if not any(pids(x) for x in [p["pat"], p.get("child")] if x):
            return
        time.sleep(0.5)
    for pid in pids(p["pat"]) + (pids(p["child"]) if p.get("child") else []):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def age(path):
    try:
        return time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return None


def watch_log_age():
    ages = [a for a in (age(p) for p in (ROOT / "logs").glob("demo-watch-*.log")) if a is not None]
    return min(ages) if ages else None


def notify(st, issue, fixes):
    last = st.setdefault("notified", {}).get(issue, 0)
    if time.time() - last < NOTIFY_COOLDOWN:
        log(f"问题 {issue} 1 小时内已通知过，本次只记日志")
        return
    try:
        from desk import demo_ws
        url, key = demo_ws._notify_cfg()
        if not url:
            log("webhook 未配置，未通知"); return
        import requests
        h = {"Content-Type": "application/json"}
        if key:
            h["Authorization"] = f"Bearer {key}"; h["X-Webhook-Key"] = key
        body = {"event_type": "health_fix", "issue": issue, "actions": fixes, "message": "健康检查已自动修复：" + "；".join(fixes),
                "time": now_str(), "timezone": "Asia/Shanghai (北京时间)", "source": "ta-desk-healthcheck"}
        r = requests.post(url, data=json.dumps(body, ensure_ascii=False).encode(), headers=h, timeout=20)
        log(f"已通知 health_fix {issue} HTTP {r.status_code}")
        st["notified"][issue] = time.time()
    except Exception as e:
        log(f"通知失败 {type(e).__name__}")


def main():
    os.chdir(ROOT)
    st = {}
    try:
        st = json.loads(STATE.read_text())
    except Exception:
        pass
    gap = time.time() - st.get("last_run", 0)
    paused = st.get("last_run") and gap > PAUSE_GAP
    st["last_run"] = time.time()
    if not (ROOT / ".venv/bin/python").exists():
        log("错误：.venv/bin/python 不存在，无法修复（需人工）")
        notify(st, "venv_missing", [".venv/bin/python 不存在，未能自动修复，需要人工处理"])
        STATE.write_text(json.dumps(st)); return 1
    fixes = {}
    for name, p in PROCS.items():
        alive = bool(pids(p["pat"])) and (not p.get("child") or bool(pids(p["child"])))
        if not alive:
            stop(name); start(name)
            fixes[f"{name}_down"] = f"{name} 进程不在，已 setsid nohup 重启"
            continue
        a = age(HEARTBEAT) if name == "demo_ws" else watch_log_age()
        if a is not None and a > STALE:
            if paused:
                log(f"{name} {int(a/60)} 分钟未更新，但距上次检查 {int(gap/60)} 分钟（机器刚恢复），本轮不判定")
                continue
            stop(name); start(name)
            fixes[f"{name}_stale"] = f"{name} {'心跳' if name == 'demo_ws' else '日志'} {int(a/60)} 分钟未更新，已重启"
    if fixes:
        time.sleep(5)
        for k, v in fixes.items():
            n = k.rsplit("_", 1)[0]
            ok = bool(pids(PROCS[n]["pat"]))
            log(f"修复：{v}；重启后存活={ok}")
            notify(st, k, [v + ("" if ok else "（重启后仍未存活！）")])
    else:
        log(f"正常：demo_watch/demo_ws 在运行，心跳 {int((age(HEARTBEAT) or 0))}s 前，watch 日志 {int(watch_log_age() or 0)}s 前")
    STATE.write_text(json.dumps(st))
    try:                                   # O1/O2 链路检查与告警（纯文件检查，不重启、不下单）
        from desk import chain_health, settings as S
        sent = chain_health.run(S.load_config())
        if sent:
            log("链路告警：" + "、".join(sent))
    except Exception as e:
        log(f"链路检查失败 {type(e).__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
