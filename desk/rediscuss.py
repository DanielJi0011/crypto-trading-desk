"""止损或止盈后再讨论：对单个币跑一遍完整的每日多角色流程（全部分析师→多空→研究经理→交易员→三风控→组合经理→交易经理），
经统一的加权聚合与组合经理执行设定下一次入场（现价单或条件单）。由 desk.demo_watch 检测到模拟盘止损后在后台启动。

用法：python -m desk.rediscuss --coin BTC [--event 平仓编号] [--reason sl|tp] [--mock]"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from desk import settings


REASON_CN = {"sl": "止损", "tp": "止盈", "rr_recheck": "盈亏比复核"}


def _fmt(x) -> str:
    return "未知" if x is None else f"{float(x):.6g}"


def reason_brief(reason: str, event: str, detail: dict | None = None) -> str:
    """传给全部角色的触发原因说明。rr_recheck 时 detail 为开仓后 RR 复核的事实包（desk/rr_recheck.py 写入）。"""
    if reason == "rr_recheck":
        d = detail or {}
        return (f"## 本次再讨论的触发原因：开仓后盈亏比复核（事件 {event}）\n"
                f"刚开仓的这笔仓位实际成交价偏离了计划入场价：计划入场 {_fmt(d.get('planned_entry'))}"
                f"（限价 {_fmt(d.get('limit_px'))}，按计划价算的盈亏比 RR={_fmt(d.get('planned_rr'))}），"
                f"实际成交均价 {_fmt(d.get('fill'))}；止损 {_fmt(d.get('sl'))}（距成交价 {_fmt(d.get('sl_dist'))}）、"
                f"止盈 {_fmt(d.get('tp'))}（距成交价 {_fmt(d.get('tp_dist'))}）是下单时按计划价定的绝对价，"
                f"没有随实际成交价移动，因此按实际成交价计算的盈亏比只有 RR={_fmt(d.get('rr'))}，"
                f"低于护栏下限 {_fmt(d.get('min_rr'))}。\n"
                "本次讨论的任务：处置这笔已经开着的仓位——调整止损止盈（止损只许收紧）、减仓或平仓，"
                "或者明确说明保持不变的理由。不要把本次复核当作反向开仓信号，也不要同向加仓放大这笔仓位的风险。")
    if reason == "tp":
        return (f"## 本次再讨论的触发原因：止盈成交（事件 {event}）\n"
                "上一笔仓位刚在止盈价平仓，本次讨论的任务是定下一次入场点位。注意避免追单：价格已朝原方向走完一段，"
                "不要在现价直接追同向；优先考虑等回抽到关键位再入场的条件单，或者确认反转再反向，没有好位置就观望。")
    return (f"## 本次再讨论的触发原因：止损成交（事件 {event}）\n"
            "上一笔仓位刚被止损，本次讨论的任务是重新评估并定下一次入场点位；同向开仓有冷却期限制。")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--coin", required=True)
    ap.add_argument("--event", default="")
    ap.add_argument("--reason", choices=("sl", "tp", "rr_recheck"), default="sl")   # 触发原因：止损 / 止盈 / 开仓后 RR 复核
    ap.add_argument("--mock", action="store_true")
    a = ap.parse_args(argv)
    if a.mock:
        os.environ["TA_DESK_MOCK_LLM"] = "1"
    logging.basicConfig(level=logging.WARNING)
    from desk import analysis, ledger, manager, okx_data, pending, roles, scoring, stances
    from desk import llm as dllm
    from desk import okx_demo as _X
    _X.set_event_source("rediscuss")   # O3
    cfg = settings.load_config()
    ledger.assert_paper(cfg)
    if settings.halt_active():   # R7：HALT 全冻结下不做任何自动决策与写操作
        print(f"[rediscuss {settings.now_str()}] HALT 全冻结生效，跳过再讨论", flush=True); return 0
    coin = next(c for c in cfg["coins"] if c["inst_id"].split("-")[0] == a.coin.upper())
    if os.environ.get("TA_DESK_MOCK_LLM") != "1" and not dllm.prepare_env(cfg):
        print("错误：未设置 LLM 密钥", file=sys.stderr); return 2
    if os.environ.get("TA_DESK_MOCK_LLM") != "1":
        from desk import llm_retry
        llm_retry.install(cfg)   # 503/超时/连接错误指数退避重试
    roles.ensure_playbooks()
    tracker = dllm.UsageTracker(cfg)
    date = settings.today()
    print(f"[rediscuss {settings.now_str()}] {coin['inst_id']} {REASON_CN[a.reason]}事件 {a.event}：开始全角色讨论", flush=True)
    from desk import demo_exec, okx_demo
    prices = {c["inst_id"]: okx_data.last_price(c["inst_id"]) for c in cfg["coins"]}
    if demo_exec.is_demo(cfg):
        okx_demo.startup(cfg)
    bk = demo_exec.book(cfg, prices) if demo_exec.is_demo(cfg) else ledger.book(cfg, prices)
    detail = None
    if a.reason == "rr_recheck" and a.event:   # 事实包由 monitor.schedule_rediscuss 随事件存进 rediscuss.json
        from desk import monitor as _mon0
        _st = settings.read_json(_mon0.REDISCUSS, {})
        detail = ((_st.get(coin["inst_id"]) or {}).get("details") or {}).get(a.event)
    res = analysis.analyze(cfg, coin, date, bk, tracker, extra_brief=reason_brief(a.reason, a.event, detail))
    fs = res["final_state"]; fs["_signal"] = res["signal"]
    st = stances.extract(fs, cfg, tracker)
    _src = {"tp": "post_tp", "sl": "post_stop", "rr_recheck": "rr_recheck"}[a.reason]
    rec = manager.decide_and_execute(cfg, coin, date, fs, st, tracker, source=f"{_src}:{a.event}")
    rec["trigger_reason"] = a.reason
    rec["report_dir"] = res["report_dir"]
    scoring.record_stances(cfg, rec)
    usage = tracker.summary()
    settings.write_json(settings.STATE / "usage" / f"rediscuss-{dllm.RUN_ID}.json", usage)
    okx_demo.log_event("rediscuss_done", inst_id=coin["inst_id"], trade_id=a.event, reason=a.reason, decision_id=rec["decision_id"], action=rec["final_action"],
                  consensus=rec["consensus"], cost_usd=usage.get("cost_usd"), calls=usage.get("calls"))
    if a.event:   # 决策已落盘 → 把事件从在途标记为已完成（monitor 的续跑语义靠这一步；HALT/异常退出不走这里，会被超时续跑）
        from desk import monitor as _mon
        _mon.complete_rediscuss(coin["inst_id"], a.event)
    print(f"[rediscuss] {coin['inst_id']}（{REASON_CN[a.reason]}后）S={rec['consensus']:+.3f} → {rec['final_action']}；{'；'.join(rec['notes'])}；"
          f"LLM {usage['calls']} 次 ${usage['cost_usd']:.4f}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
