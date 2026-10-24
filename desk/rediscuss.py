"""止损或止盈后再讨论：对单个币跑一遍完整的每日多角色流程（全部分析师→多空→研究经理→交易员→三风控→组合经理→交易经理），
经统一的加权聚合与组合经理执行设定下一次入场（现价单或条件单）。由 desk.demo_watch 检测到模拟盘止损后在后台启动。

用法：python -m desk.rediscuss --coin BTC [--event 平仓编号] [--reason sl|tp] [--mock]"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from desk import settings


REASON_CN = {"sl": "止损", "tp": "止盈"}


def reason_brief(reason: str, event: str) -> str:
    """传给全部角色的触发原因说明。"""
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
    ap.add_argument("--reason", choices=("sl", "tp"), default="sl")   # 触发原因：止损 / 止盈
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
    res = analysis.analyze(cfg, coin, date, bk, tracker, extra_brief=reason_brief(a.reason, a.event))
    fs = res["final_state"]; fs["_signal"] = res["signal"]
    st = stances.extract(fs, cfg, tracker)
    rec = manager.decide_and_execute(cfg, coin, date, fs, st, tracker, source=f"{'post_tp' if a.reason == 'tp' else 'post_stop'}:{a.event}")
    rec["trigger_reason"] = a.reason
    rec["report_dir"] = res["report_dir"]
    scoring.record_stances(cfg, rec)
    usage = tracker.summary()
    settings.write_json(settings.STATE / "usage" / f"rediscuss-{dllm.RUN_ID}.json", usage)
    okx_demo.log_event("rediscuss_done", inst_id=coin["inst_id"], trade_id=a.event, reason=a.reason, decision_id=rec["decision_id"], action=rec["final_action"],
                  consensus=rec["consensus"], cost_usd=usage.get("cost_usd"), calls=usage.get("calls"))
    print(f"[rediscuss] {coin['inst_id']}（{REASON_CN[a.reason]}后）S={rec['consensus']:+.3f} → {rec['final_action']}；{'；'.join(rec['notes'])}；"
          f"LLM {usage['calls']} 次 ${usage['cost_usd']:.4f}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
