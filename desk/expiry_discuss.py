"""持有期到期 → 对该币跑一次与早晚报相同的全员讨论（2026-10-30 用户决定；expiry_extend.mode=full_discussion，默认）。

流程（复用现有单币流程，不加新提示词模板）：analysis.analyze（全部分析师→多空→研究经理→交易员→三风控，持仓说明 position_brief
全员共享，另附一段到期事实：已持有天数/原持有期/到期时刻/已延期次数）→ stances.extract → manager.decide_and_execute
（ATR+2R 基准 + 全员按 权重×置信度 加权调整、交易经理最终调整与否决、组合经理执行并过护栏/价格漂移检查，规则与早晚报完全相同）。

结果判定（本进程只写结果文件，持仓登记由 demo_watch 单写者更新，避免两个进程互相覆盖登记）：
- 交易经理 sell → 已由组合经理平仓：本进程记 expiry_closed 事件并通知；
- 其余（hold / reduce / 调止损止盈 / 加仓等，按早晚报规则已执行）且仓位仍在 → 视为延期：demo_watch 读结果后
  deadline = 原到期时刻 + 本次决策 horizon_days（缺省 5），extensions+1，记 expiry_extended 并通知；
- 任何异常（LLM 已按 llm_retry 重试 3 次仍失败）、交易经理缺席（tm_status=absent）、HALT → 结果 failed，demo_watch 回退到期平仓。
事件来源 TA_EVENT_SOURCE=expiry：不计入早晚报每日下单数；决策记录 session=expiry、source=expiry:<deadline>；不写 stances（不参与评分）。
!! 只经 desk/demo_exec（模拟盘）执行，无任何实盘下单路径。

用法：python -m desk.expiry_discuss --coin BTC --deadline "YYYY-MM-DD HH:MM:SS+0800" [--mock]"""
from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import fcntl
import json
import logging
import os
import re
import subprocess
import sys

from desk import settings

FMT = "%Y-%m-%d %H:%M:%S%z"
DIR = settings.STATE / "expiry_discuss"


def _key(inst: str, deadline: str) -> str:
    return re.sub(r"[^0-9A-Za-z_-]", "", f"{inst}-{deadline}")


def result_path(inst: str, deadline: str):
    return DIR / f"{_key(inst, deadline)}.json"


@contextlib.contextmanager
def _flock(path, block=True):
    DIR.mkdir(parents=True, exist_ok=True)
    f = open(path, "w")
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if block else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False; return
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    finally:
        f.close()


def read_result(inst, deadline) -> dict | None:
    return settings.read_json(result_path(inst, deadline), None)


def write_result_once(inst, deadline, data: dict) -> dict:
    """结果只写一次（文件锁内先查后写）；已有结果则返回已有的。"""
    p = result_path(inst, deadline)
    with _flock(str(p) + ".lock"):
        old = settings.read_json(p, None)
        if old:
            return old
        data = {"inst_id": inst, "deadline": deadline, "time": settings.now_str(), **data}
        settings.write_json(p, data)
        return data


def SPAWN(args: list[str], logfile):   # 与 monitor.SPAWN（止损后再讨论）同一后台方式：独立会话子进程，不阻塞盯盘
    with open(logfile, "a") as f:
        subprocess.Popen([sys.executable, "-m", "desk.expiry_discuss", *args], cwd=str(settings.CODE), stdout=f,
                         stderr=subprocess.STDOUT, start_new_session=True, env=os.environ.copy())


def schedule(cfg, r: dict, inst: str, m: dict, save) -> bool:
    """demo_watch 调用：在 deadline 上只触发一次。先写标记（expiry_discussed_for）落盘，再启动后台进程。返回是否本次启动。"""
    key = m["deadline"]
    with _flock(DIR / "schedule.lock") as ok:
        if not ok or m.get("expiry_discussed_for") == key:
            return False
        m["expiry_discussed_for"] = key
        m["expiry_discussion"] = {"deadline": key, "started": settings.now_str(), "status": "started"}
        r.setdefault("positions", {})[inst] = m
        save(r)
        settings.LOGS.mkdir(parents=True, exist_ok=True)
        SPAWN(["--coin", inst.split("-")[0], "--deadline", key], settings.LOGS / f"expiry-discuss-{settings.today()}.log")
    return True


def held_days(m: dict, now) -> float | None:
    for k in ("entry_filled_time", "opened"):
        try:
            return round((now - _dt.datetime.strptime(m[k], FMT)).total_seconds() / 86400, 2)
        except Exception:
            continue
    return None


def brief(m: dict, deadline: str, now) -> str:
    """附给全体角色的到期事实（与止损后再讨论的触发原因说明同一机制 extra_brief）。持仓方向/张数/均价/止损止盈/浮盈亏见共享持仓说明。"""
    return (f"## 本次讨论的触发原因：该币持仓持有期到期（到期时刻 {deadline}）\n"
            f"已持有 {held_days(m, now)} 天，原持有期 {m.get('horizon_days', 5)} 天，已延期 {int(m.get('extensions') or 0)} 次。"
            "按早晚报同样的规则给出对该持仓的决策：继续持有（即延期，新的持有期取本次决策周期）、平仓、减仓或调整止损止盈。")


def run(cfg, coin: dict, deadline: str, tracker=None) -> dict:
    """进程内主体（测试可直接调用）。返回写入的结果。"""
    from desk import analysis, demo_exec, ledger, manager, okx_data, stances
    from desk import demo_exec as DX
    from desk import llm as dllm
    from desk import okx_demo as X
    inst = coin["inst_id"]
    if read_result(inst, deadline):
        return read_result(inst, deadline)
    if settings.halt_active():
        return write_result_once(inst, deadline, {"status": "failed", "reason": "HALT 全冻结生效，未讨论"})
    m = (DX.reg().get("positions") or {}).get(inst) or {}
    tracker = tracker or dllm.UsageTracker(cfg)
    date = settings.today()
    try:
        from desk import drift
        prices = {c["inst_id"]: okx_data.last_price(c["inst_id"]) for c in cfg["coins"]}
        bk = demo_exec.book(cfg, prices) if demo_exec.is_demo(cfg) else ledger.book(cfg, prices)
        if not any(p["inst_id"] == inst for p in bk["positions"]):
            return write_result_once(inst, deadline, {"status": "gone", "reason": "讨论开始时仓位已不存在"})
        ap = drift.stamp(inst)
        res = analysis.analyze(cfg, coin, date, bk, tracker, extra_brief=brief(m, deadline, settings.now()))
        fs = res["final_state"]; fs["_signal"] = res["signal"]; fs["_analysis_px"] = ap
        tok = dllm.STAGE.set(f"{inst}/立场抽取")
        try:
            st = stances.extract(fs, cfg, tracker)
        finally:
            dllm.STAGE.reset(tok)
        if read_result(inst, deadline):   # 盯盘已判超时并回退平仓：不再执行
            return read_result(inst, deadline)
        rec = manager.decide_and_execute(cfg, coin, date, fs, st, tracker, source=f"expiry:{deadline}")
    except Exception as e:
        X.log_event("expiry_discussion_failed", inst_id=inst, deadline=deadline, error=f"{type(e).__name__}: {str(e)[:200]}")
        return write_result_once(inst, deadline, {"status": "failed", "reason": f"全员讨论失败（{type(e).__name__}），回退到期平仓"})
    # 注：不调用 scoring.record_stances —— 评分只按早/晚场口径，到期场不计入评分（见回报）
    base = {"decision_id": rec["decision_id"], "final_action": rec["final_action"], "consensus": rec["consensus"],
            "tm_status": rec.get("tm_status"), "tm_action": (rec.get("manager") or {}).get("action"),
            "horizon_days": float((rec.get("aggregate") or {}).get("horizon_days") or 5) if rec.get("aggregate") else 5.0,
            "notes": (rec.get("notes") or [])[:6]}
    if rec.get("tm_status") == "absent":
        return write_result_once(inst, deadline, {**base, "status": "failed", "reason": "交易经理缺席（调用失败），回退到期平仓"})
    if rec["final_action"] == "sell":
        out = write_result_once(inst, deadline, {**base, "status": "done", "outcome": "closed",
                                                 "reason": f"全员讨论结论平仓（S={rec['consensus']:+.3f}）"})
        X.log_event("expiry_closed", inst_id=inst, deadline=deadline, reason=out["reason"], mode="full_discussion",
                    decision_id=rec["decision_id"], consensus=rec["consensus"])
        _notify("position_expired", inst_id=inst, message=f"{inst} 持有期到期全员讨论：平仓（共识 S={rec['consensus']:+.3f}，决策 {rec['decision_id']}）")
        return out
    outcome = "adjusted" if rec["final_action"] != "hold" or any("止损" in n or "止盈" in n for n in base["notes"]) else "held"
    return write_result_once(inst, deadline, {**base, "status": "done", "outcome": outcome,
                                              "reason": f"全员讨论结论{'持有' if outcome == 'held' else '调整后持有'}（{rec['final_action']}，S={rec['consensus']:+.3f}）"})


def _notify(kind, **kw):
    try:
        from desk.demo_ws import notify
        notify({"kind": kind, **kw})
    except Exception:
        pass


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--coin", required=True)
    ap.add_argument("--deadline", required=True)
    ap.add_argument("--mock", action="store_true")
    a = ap.parse_args(argv)
    if a.mock:
        os.environ["TA_DESK_MOCK_LLM"] = "1"
    logging.basicConfig(level=logging.WARNING)
    from desk import ledger, roles
    from desk import llm as dllm
    from desk import okx_demo as X
    X.set_event_source("expiry")
    cfg = settings.load_config()
    ledger.assert_paper(cfg)
    coin = next(c for c in cfg["coins"] if c["inst_id"].split("-")[0] == a.coin.upper())
    inst = coin["inst_id"]
    with _flock(str(result_path(inst, a.deadline)) + ".run", block=False) as ok:
        if not ok:
            print(f"[expiry_discuss] {inst} {a.deadline} 已有讨论在运行，退出", flush=True); return 0
        if os.environ.get("TA_DESK_MOCK_LLM") != "1":
            if not dllm.prepare_env(cfg):
                write_result_once(inst, a.deadline, {"status": "failed", "reason": "未设置 LLM 密钥，回退到期平仓"}); return 2
            from desk import llm_retry
            llm_retry.install(cfg)   # 503/超时/连接错误重试（沿用现有重试机制）
        roles.ensure_playbooks()
        from desk import demo_exec, okx_demo
        if demo_exec.is_demo(cfg):
            okx_demo.startup(cfg)
        tracker = dllm.UsageTracker(cfg)
        print(f"[expiry_discuss {settings.now_str()}] {inst} 持有期到期 {a.deadline}：开始全员讨论", flush=True)
        out = run(cfg, coin, a.deadline, tracker)
        usage = tracker.summary()
        settings.write_json(settings.STATE / "usage" / f"expiry-{dllm.RUN_ID}.json", usage)
        print(f"[expiry_discuss] {inst} → {out.get('status')} {out.get('outcome', '')} {out.get('reason', '')}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
