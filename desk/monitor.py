"""止损/止盈后再讨论的调度器（schedule_rediscuss / complete_rediscuss），供 demo_watch（轮询）、
demo_ws（WebSocket）与 rr_recheck 共用：事件去重、在途标记、6h 冷却、子进程启动都在这里，
状态文件 state/ledger/rediscuss.json，文件锁保证跨进程同一事件只触发一次。

历史：本文件原是本地纸面盘中监控（止损/止盈盯市 + 条件单触发，process_pending/run_once/main），
用户 2026-10-20 停用 run_monitor.sh 后再未运行，2026-11-15 代码审查后纸面监控部分退休删除；
模拟盘的止损检测见 desk/demo_watch.py / desk/demo_ws.py。"""
from __future__ import annotations

import datetime as _dt
import os
import subprocess
import sys

from desk import settings

REDISCUSS = settings.STATE / "ledger" / "rediscuss.json"


def PCFG(cfg) -> dict:
    d = {"rediscuss_after_stop": True, "rediscuss_cooldown_hours": 6}
    return {**d, **(cfg.get("pending") or {})}


def SPAWN(args: list[str], logfile):
    with open(logfile, "a") as f:
        subprocess.Popen([sys.executable, "-m", "desk.rediscuss", *args], cwd=str(settings.CODE), stdout=f,
                         stderr=subprocess.STDOUT, start_new_session=True, env=os.environ.copy())


# ---------------- 止损后再讨论（后台、每个止损事件至多一次 + 冷却）
def schedule_rediscuss(cfg, trade: dict) -> str:
    """止损/止盈→再讨论（trade["reason"] 为 "sl" 或 "tp"，缺省 "sl"）；reason="rr_recheck" 为开仓后 RR 复核
    （desk/rr_recheck.py，trade_id='DEMO-<posId>'，可带 trade["detail"] 事实包随事件存档；豁免跨事件冷却，见下）。
    demo_watch（轮询）与 demo_ws（WebSocket）共用：trade_id 统一为 'DEMO-<algoId>'，
    已完成记录、在途记录与 6h 冷却都在 rediscuss.json；用文件锁保证跨进程同一笔止损只触发一次。
    标记语义（2026-11-09）：启动只记在途，子进程决策落盘后由 complete_rediscuss 移入已完成；在途超时（REDISCUSS_STALE_HOURS）视为进程已死可续跑。"""
    import fcntl
    REDISCUSS.parent.mkdir(parents=True, exist_ok=True)
    with open(str(REDISCUSS) + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            return _schedule_rediscuss_locked(cfg, trade)
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


REDISCUSS_STALE_HOURS = 2.0   # 在途超过此时长未完成 → 视为子进程已死，可续跑


def complete_rediscuss(inst: str, trade_id: str) -> bool:
    """再讨论子进程在决策落盘后调用（desk/rediscuss.py）：把事件从在途移到已完成。
    旧流程在启动时就标记已处理，子进程被杀会把这次再讨论永久吞掉；2026-11-09 改为
    启动只记在途（inflight），完成落盘后才进 events，在途超时未完成可被重新启动续跑。"""
    import fcntl
    REDISCUSS.parent.mkdir(parents=True, exist_ok=True)
    with open(str(REDISCUSS) + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            st = settings.read_json(REDISCUSS, {})
            s = st.setdefault(inst, {"events": [], "last": None})
            inflight = s.setdefault("inflight", {})
            if trade_id not in inflight:
                return False
            del inflight[trade_id]
            if trade_id not in s["events"]:
                s["events"].append(trade_id)
            if s.get("details"):
                s["details"].pop(trade_id, None)   # rr_recheck 的事实包随完成一并清理
            settings.write_json(REDISCUSS, st)
            return True
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def _schedule_rediscuss_locked(cfg, trade: dict) -> str:
    from desk import okx_demo, pending
    P = PCFG(cfg)
    inst = trade["inst_id"]
    if not P["rediscuss_after_stop"]:
        return "关闭"
    st = settings.read_json(REDISCUSS, {})
    s = st.setdefault(inst, {"events": [], "last": None})
    inflight = s.setdefault("inflight", {})
    if trade["trade_id"] in s["events"]:
        return "该事件已讨论过"
    now = settings.now()
    reason = trade.get("reason") if trade.get("reason") in ("tp", "rr_recheck", "trailing_stop", "breakeven_lock") else "sl"

    def _spawn() -> str:
        prev_last = s.get("last")
        s["last"] = settings.now_str()
        inflight[trade["trade_id"]] = s["last"]
        if trade.get("detail"):
            s.setdefault("details", {})[trade["trade_id"]] = trade["detail"]   # rr_recheck：事实包随事件存档，rediscuss 子进程读取
        settings.write_json(REDISCUSS, st)
        settings.LOGS.mkdir(parents=True, exist_ok=True)
        try:
            SPAWN(["--coin", inst.split("-")[0], "--event", trade["trade_id"], "--reason", reason],
                  settings.LOGS / f"rediscuss-{settings.today()}.log")
        except Exception as e:   # 启动失败不留假在途、也不起冷却，下个巡检周期可重试
            inflight.pop(trade["trade_id"], None)
            if s.get("details"):
                s["details"].pop(trade["trade_id"], None)
            s["last"] = prev_last
            settings.write_json(REDISCUSS, st)
            okx_demo.log_event("rediscuss_spawn_failed", inst_id=inst, trade_id=trade["trade_id"], reason=str(e)[:200])
            return "启动失败"
        pending.event("再讨论启动", inst, trade_id=trade["trade_id"], reason=reason)
        okx_demo.log_event("rediscuss_started", inst_id=inst, trade_id=trade["trade_id"], reason=reason)
        return "已启动"

    started = inflight.get(trade["trade_id"])
    if started:
        try:
            age = now - _dt.datetime.strptime(started, "%Y-%m-%d %H:%M:%S%z")
        except (TypeError, ValueError):
            age = None
        if age is not None and age < _dt.timedelta(hours=REDISCUSS_STALE_HOURS):
            return "再讨论进行中"
        # 同一事件的续跑不受冷却约束：冷却只拦新事件，续跑被冷却挡住等于又把这次讨论吞掉
        okx_demo.log_event("rediscuss_resume", inst_id=inst, trade_id=trade["trade_id"], reason="在途超时未完成，续跑")
        return _spawn()
    # rr_recheck 豁免跨事件冷却：冷却是为止损后防 LLM 连环触发设的；RR 复核是活仓位几何异常的安全复核，
    # 且每仓至多一次（rr_recheck.json 的 pkey 状态 + 此处 events 去重双重保证），被冷却吞掉等于没补上这个复核。
    if reason != "rr_recheck" and s["last"] and now - _dt.datetime.strptime(s["last"], "%Y-%m-%d %H:%M:%S%z") < _dt.timedelta(hours=float(P["rediscuss_cooldown_hours"])):
        s["events"].append(trade["trade_id"])   # 冷却跳过与旧行为一致：记为已处理，不再重试
        settings.write_json(REDISCUSS, st)
        pending.event("再讨论跳过", inst, trade_id=trade["trade_id"], reason=f"冷却期 {P['rediscuss_cooldown_hours']}h 内", trigger=trade.get("reason", "sl"))
        okx_demo.log_event("rediscuss_skipped", inst_id=inst, trade_id=trade["trade_id"], reason=f"冷却期 {P['rediscuss_cooldown_hours']}h 内")
        return "冷却中"
    return _spawn()
