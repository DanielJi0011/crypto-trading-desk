"""席位级续跑（2026-11-15 用户批准）：某个席位的模型线路中断（典型：opus 中继
503）把一场运行打断后，不再整场重跑，而是逐币只补缺失的环节。

一句话语义：**决策记录在 → 跳过；图产物在 → 只补尾段（共识→判定→交易经理→
执行）；图存档点在 → 从最后完成的图节点续；都没有 → 该币整币新跑。**

三层机制：
1. 图内续跑：TradingAgents 的 LangGraph 存档点（vendor checkpointer，每 ticker
   一个 SQLite、按 ticker+日期+签名定线程）。启用条件见 desk/llm.checkpoint_enabled
   ——只在串行上下文开（定时场次是三币并行、共用一个图实例，存档点会重编译图，
   与并行互斥，run_daily 并行守卫同理）；续跑入口强制串行并置 settings.RESUME_MODE。
   成功跑完的图由 vendor 在收尾时自行清存档点；fresh 运行（定时场次/整场重跑）
   由 desk/analysis 在 propagate 前清掉本线程，保证从干净状态开始。
   签名里含组合指纹（现金+持仓），而模拟盘现金随实时权益浮动：崩溃后隔一段时间
   用现读账本几乎必然指纹不符、续跑落空。因此 analyze 在每次启用存档点的图运行
   前把当次 PortfolioContext 原样存进 run-ctx 旁车，续跑时喂回同一份组合，签名
   才能对上。执行环节（交易经理/护栏）读的是实时账本，不受这份分析快照影响。
2. 尾段续跑：图跑完时 vendor 把 final_state 写进
   state/ta_results/<TICKER>/TradingAgentsStrategy_logs/full_states_log_<日期>.json，
   但图外字段（资金流报告 _flows_report、_x_feed、_flows_missing、信号）不在其中，
   缺了它们立场抽取与数据缺失标记都会失真。因此 analyze 在图完成后把这几个字段
   连同图状态的哈希写进 tail 旁车（desk_tail_<场次键>.json，无条件写、失败降级）。
   尾段重建时校验旁车哈希与现存 full_states_log 一致（防早晚场同日覆盖串场），
   不一致则拒绝尾段、回落到图路径。
3. 跳过：state/decisions.jsonl 里已有本场次（date+session+inst）决策记录即视为
   该币已完成。注意窗口：决策记录在 decide_and_execute 末尾才落盘，若崩溃恰好
   发生在「已执行下单、未落盘」之间，续跑会重新走交易经理——与整场重跑同等风险，
   靠交易经理能看到实时持仓/挂单与一币一仓护栏兜底。
"""
from __future__ import annotations

import hashlib
import json
import logging

from desk import sessions, settings

log = logging.getLogger("ta-desk.resume")

# vendor _log_state 落盘的图状态字段（哈希与重建都以此为准，run_settings 不计）
_SHA_FIELDS = ("company_of_interest", "trade_date", "market_report", "sentiment_report",
               "news_report", "fundamentals_report", "trader_investment_plan",
               "investment_plan", "final_trade_decision")
_SHA_DEBATE = ("bull_history", "bear_history", "history", "current_response")
_SHA_RISK = ("aggressive_history", "conservative_history", "neutral_history", "history")


def state_sha_of(m: dict) -> str:
    """图状态实质字段的哈希：tail 旁车写入时与重建校验时共用同一口径。"""
    canon = {k: m.get(k) for k in _SHA_FIELDS}
    ids, rds = m.get("investment_debate_state") or {}, m.get("risk_debate_state") or {}
    canon["investment_debate_state"] = {k: ids.get(k) for k in _SHA_DEBATE}
    canon["risk_debate_state"] = {k: rds.get(k) for k in _SHA_RISK}
    raw = json.dumps(canon, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _logs_dir(ticker: str):
    from tradingagents.dataflows.symbols import safe_ticker_component
    return settings.STATE / "ta_results" / safe_ticker_component(ticker) / "TradingAgentsStrategy_logs"


def full_state_log_path(ticker: str, date: str):
    return _logs_dir(ticker) / f"full_states_log_{date}.json"


def tail_sidecar_path(ticker: str, date: str, session: str):
    return _logs_dir(ticker) / f"desk_tail_{sessions.key(date, session)}.json"


def run_ctx_path(ticker: str, date: str, session: str):
    return _logs_dir(ticker) / f"desk_run_ctx_{sessions.key(date, session)}.json"


# ------------------------------------------------------------------ run-ctx 旁车（图运行的组合快照）
def write_run_ctx(ticker: str, date: str, session: str, portfolio, signature) -> None:
    settings.write_json(run_ctx_path(ticker, date, session), {
        "date": date, "session": sessions.norm(session), "ticker": ticker,
        "portfolio": portfolio.model_dump(), "signature": signature})


def read_run_ctx(ticker: str, date: str, session: str) -> dict | None:
    ctx = settings.read_json(run_ctx_path(ticker, date, session), None)
    if not ctx or ctx.get("date") != date or ctx.get("session") != sessions.norm(session):
        return None
    return ctx


# ------------------------------------------------------------------ tail 旁车（图外字段 + 状态哈希）
def write_tail_sidecar(ticker: str, date: str, session: str, fs: dict, signal) -> None:
    settings.write_json(tail_sidecar_path(ticker, date, session), {
        "date": date, "session": sessions.norm(session), "ticker": ticker,
        "state_sha": state_sha_of(fs),
        "_flows_report": fs.get("_flows_report", ""),
        "_x_feed": bool(fs.get("_x_feed")),
        "_flows_missing": bool(fs.get("_flows_missing")),
        "signal": signal})


def load_tail_fs(coin: dict, date: str, session: str) -> dict | None:
    """图产物齐全且旁车哈希对得上时，重建可直接喂给立场抽取/交易经理的 final_state；
    任一条件不满足返回 None（回落到图路径）。_analysis_px 不重建：漂移检查按设计
    在缺失时跳过（decide_and_execute 原生支持）。"""
    ticker = coin["ticker"]
    entry = settings.read_json(full_state_log_path(ticker, date), None)
    sc = settings.read_json(tail_sidecar_path(ticker, date, session), None)
    if not entry or not sc:
        return None
    if entry.get("trade_date") != date or sc.get("date") != date or sc.get("session") != sessions.norm(session):
        return None
    if sc.get("state_sha") != state_sha_of(entry):
        log.info("尾段续跑拒绝：%s %s 的 full_states_log 与旁车哈希不一致（疑似被同日另一场覆盖）",
                 ticker, date)
        return None
    fs = {k: entry.get(k) for k in _SHA_FIELDS}
    fs["investment_debate_state"] = entry.get("investment_debate_state") or {}
    fs["risk_debate_state"] = entry.get("risk_debate_state") or {}
    fs["_flows_report"] = sc.get("_flows_report", "")
    fs["_x_feed"] = bool(sc.get("_x_feed"))
    fs["_flows_missing"] = bool(sc.get("_flows_missing"))
    fs["_signal"] = sc.get("signal")
    return fs


# ------------------------------------------------------------------ 已完成判定
def find_decision(date: str, session: str, inst_id: str) -> dict | None:
    """decisions.jsonl 里本场次该币的决策记录（取最后一条）；再讨论/到期讨论的
    记录 session 为 rediscuss/expiry，不会误命中 morning/evening。"""
    from desk import manager
    if not manager.DECISIONS.exists():
        return None
    found = None
    for r in settings.read_jsonl(manager.DECISIONS):
        if r.get("date") == date and r.get("session") == sessions.norm(session) and r.get("inst_id") == inst_id:
            found = r
    return found


# ------------------------------------------------------------------ 单币续跑编排
def resume_coin(cfg, coin: dict, date: str, tracker):
    """返回 (决策记录, 路径)：done=已有决策跳过 / tail=只补尾段 /
    graph-resume=图从存档点续 / graph-fresh=整币新跑（本次带存档点，中途再断可续）。
    立场落盘（scoring.record_stances）由调用方对非 done 路径执行（done 的已在原
    运行时落盘，重复落盘会让打分样本翻倍）。"""
    from desk import llm as dllm
    from desk import manager, stances
    inst = coin["inst_id"]
    session = sessions.norm(settings.SESSION)
    old = find_decision(date, session, inst)
    if old is not None:
        print(f"[resume] {inst}：本场已有决策记录 {old.get('decision_id')}，跳过", flush=True)
        return old, "done"
    fs = load_tail_fs(coin, date, session)
    if fs is not None:
        print(f"[resume] {inst}：图产物齐全，只补尾段（立场→判定→交易经理→执行）", flush=True)
        tok = dllm.STAGE.set(f"{inst}/立场抽取")
        try:
            st = stances.extract(fs, cfg, tracker)
        finally:
            dllm.STAGE.reset(tok)
        rec = manager.decide_and_execute(cfg, coin, date, fs, st, tracker, run_path="tail")
        rec["report_dir"] = None
        return rec, "tail"
    from desk import analysis, demo_exec, drift, ledger, okx_data
    prices = {c["inst_id"]: okx_data.last_price(c["inst_id"]) for c in cfg["coins"]}
    bk = demo_exec.book(cfg, prices) if demo_exec.is_demo(cfg) else ledger.book(cfg, prices)
    ap = drift.stamp(inst)
    res = analysis.analyze(cfg, coin, date, bk, tracker, resume=True)
    fs = res["final_state"]; fs["_signal"] = res["signal"]; fs["_analysis_px"] = ap
    tok = dllm.STAGE.set(f"{inst}/立场抽取")
    try:
        st = stances.extract(fs, cfg, tracker)
    finally:
        dllm.STAGE.reset(tok)
    rec = manager.decide_and_execute(cfg, coin, date, fs, st, tracker,
                                     run_path="graph-resume" if res.get("resumed_from_checkpoint") else "graph-fresh")
    rec["report_dir"] = res["report_dir"]
    return rec, ("graph-resume" if res.get("resumed_from_checkpoint") else "graph-fresh")
