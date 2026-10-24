"""O1 健康检查链路 + O2 告警（纯文件检查，不调用 LLM，不发私有请求，不下单）。

run(cfg, now) 由 scripts/healthcheck.py 每轮调用（每 10 分钟）：
- kill_on/kill_off/halt_on/halt_off：state/KILL、state/HALT 存在状态发生变化时各通知一次（首次运行只记录，不通知）；文件内容含“演练”/drill 时通知标【演练】。
- evening_missing：纽约时间晚场开场后 1 小时（北京 21:30 夏令时 / 22:30 冬令时）仍无 state/daily_done/<今日>-evening → 每天一次。
- daily_missing：北京时间 alerts.daily_missing_after（默认 09:30）后仍无 state/daily_done/<今日> → 每天一次。
- chain_unhealthy：配置校验失败、止损覆盖缺口（state/stop_coverage.json missing 非空）、demo_ws 心跳超过 30 分钟、
  下单意图对账有超过 30 分钟仍未定论的记录；按问题去重，同一问题 1 小时内最多一次。
- 进程守护：demo_ws、demo_watch 不在运行 → chain_unhealthy（proc_*），只告警不拉起。
- heartbeat：alerts.heartbeat_hours > 0 时每 N 小时发一次“链路存活”；默认 0 = 关闭（见 config.yaml 注释）。
healthcheck_stale(now) 由 demo_ws 心跳调用：healthcheck 上次运行超过 30 分钟 → healthcheck_stale，1 小时最多一次。
所有通知走 demo_ws.notify（受 TA_NOTIFY_DISABLED 总开关控制）；状态写 state/chain_health.json。
"""
from __future__ import annotations

import datetime as _dt
import json
import time

from desk import settings

PATH = settings.STATE / "chain_health.json"
HC_STATE = settings.STATE / "healthcheck_state.json"
STALE_S = 1800
DEDUP_S = 3600


def acfg(cfg) -> dict:
    d = {"daily_missing_after": "09:30", "heartbeat_hours": 0}
    d.update((cfg or {}).get("alerts") or {})
    return d


def _load() -> dict:
    try:
        return json.loads(PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(d):
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def _send(kind, message, notifier):
    from desk import okx_demo as X
    X.log_event(kind, message=message)
    if notifier is None:
        from desk.demo_ws import notify as notifier
    try:
        notifier({"kind": kind, "message": message})
    except Exception:
        pass


def _dedup(st, key, now_ts) -> bool:
    n = st.setdefault("notified", {})
    if key in n and now_ts - n[key] < DEDUP_S:
        return False
    n[key] = now_ts
    return True


def _age(path, now_ts):
    try:
        return now_ts - path.stat().st_mtime
    except FileNotFoundError:
        return None


PROCS = {"demo_ws": "desk.demo_ws", "demo_watch": "run_demo_watch.sh"}   # 进程守护：只告警，不自动拉起


def running() -> set:
    """扫描 /proc 命令行，返回在跑的常驻进程名（只读，不读 environ）。"""
    import pathlib
    found = set()
    for p in pathlib.Path("/proc").glob("[0-9]*/cmdline"):
        try:
            cmd = p.read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        except Exception:
            continue
        for name, pat in PROCS.items():
            if pat in cmd:
                found.add(name)
    return found


def problems(cfg, now_ts) -> dict:
    out = {}
    try:
        up = running()
        for name in PROCS:
            if name not in up:
                out[f"proc_{name}"] = f"常驻进程 {name} 不在运行（只告警，不自动拉起）"
    except Exception as e:
        out["proc"] = f"进程检查失败：{type(e).__name__}"
    try:
        from desk import config_safety
        config_safety.validate(cfg)
    except Exception as e:
        out["config"] = f"配置校验失败：{type(e).__name__}: {str(e)[:120]}"
    try:
        from desk import stop_coverage
        miss = (stop_coverage._load().get("missing") or {})
        if miss:
            out["stop_coverage"] = "止损覆盖缺口：" + "、".join(sorted(miss))[:200]
    except Exception as e:
        out["stop_coverage"] = f"止损覆盖状态读取失败：{type(e).__name__}"
    a = _age(settings.STATE / "demo_ws_heartbeat", now_ts)
    if a is None or a > STALE_S:
        out["demo_ws"] = "demo_ws 心跳" + ("文件不存在" if a is None else f" {int(a / 60)} 分钟未更新")
    try:
        from desk import reconcile
        now = _dt.datetime.fromtimestamp(now_ts, settings.now().tzinfo)
        old = [r for r in reconcile.pending(now) if (now - reconcile._t(r["time"])).total_seconds() > STALE_S]
        if old:
            out["reconcile"] = f"{len(old)} 条下单意图超过 30 分钟未对账定论"
    except Exception as e:
        out["reconcile"] = f"对账状态读取失败：{type(e).__name__}"
    return out


def _is_drill(k: str) -> bool:
    """开关文件内容含“演练”或 drill 时，开/关两条通知都标【演练】（解除时沿用生效时记下的标记）。"""
    p = settings.HALT_PATH if k == "halt" else (settings.KILL_PATH if settings.KILL_PATH.exists() else settings.LEGACY_KILL_PATH)
    try:
        t = p.read_text(encoding="utf-8", errors="ignore")[:200].lower()
    except Exception:
        return False
    return "演练" in t or "drill" in t


def run(cfg, now: _dt.datetime | None = None, notifier=None) -> list:
    now = now or settings.now()
    ts = now.timestamp()
    st, sent = _load(), []
    A = acfg(cfg)
    flags = {"kill": settings.KILL_PATH.exists() or settings.LEGACY_KILL_PATH.exists(), "halt": settings.HALT_PATH.exists()}
    prev = st.get("flags")
    drills = st.setdefault("drill", {})
    for k, v in flags.items():
        if v:
            drills[k] = _is_drill(k)
        if prev is not None and bool(prev.get(k)) != v:
            kind = f"{k}_{'on' if v else 'off'}"
            tag = "【演练】" if drills.get(k) else ""
            _send(kind, f"{tag}{k.upper()} {'已生效' if v else '已解除'}（state/{k.upper()}）", notifier); sent.append(kind)
        if not v:
            drills.pop(k, None)
    st["flags"] = flags
    day = now.strftime("%Y-%m-%d")
    if now.strftime("%H:%M") >= A["daily_missing_after"] and not (settings.STATE / "daily_done" / day).exists() \
            and st.get("daily_missing_day") != day:
        st["daily_missing_day"] = day
        _send("daily_missing", f"{day} {A['daily_missing_after']} 后仍无日流程完成标记 state/daily_done/{day}", notifier)
        sent.append("daily_missing")
    try:   # 晚场缺失：纽约时间开场后 sessions.evening.missing_after_minutes 仍无 state/daily_done/<日期>-evening → 每天一次
        from desk import sessions
        due = sessions.missing_at(cfg, "evening", day)
        if (now >= due and not (settings.STATE / "daily_done" / f"{day}-evening").exists()
                and st.get("evening_missing_day") != day):
            st["evening_missing_day"] = day
            _send("evening_missing", f"{day} 北京 {due:%H:%M} 后仍无晚场完成标记 state/daily_done/{day}-evening", notifier)
            sent.append("evening_missing")
    except Exception:
        pass
    for key, msg in problems(cfg, ts).items():
        if _dedup(st, f"chain_unhealthy:{key}", ts):
            _send("chain_unhealthy", msg, notifier); sent.append(f"chain_unhealthy:{key}")
    hb = float(A["heartbeat_hours"] or 0)
    if hb > 0 and ts - st.get("heartbeat_ts", 0) >= hb * 3600:
        st["heartbeat_ts"] = ts
        _send("heartbeat", f"链路存活 {settings.now_str()}", notifier); sent.append("heartbeat")
    _save(st)
    return sent


def healthcheck_stale(now_ts: float | None = None, notifier=None) -> bool:
    now_ts = now_ts or time.time()
    try:
        last = json.loads(HC_STATE.read_text()).get("last_run", 0)
    except Exception:
        last = 0
    if last and now_ts - last <= STALE_S:
        return False
    st = _load()
    if not _dedup(st, "healthcheck_stale", now_ts):
        return False
    _save(st)
    _send("healthcheck_stale", "健康检查超过 30 分钟未运行（state/healthcheck_state.json）", notifier)
    return True
