"""调用 TradingAgents（v0.7.0）对单个币种做一轮完整的多角色分析。"""
from __future__ import annotations

import logging
import os

from desk import llm as dllm
from desk import okx_data, roles, settings

log = logging.getLogger("ta-desk.analysis")
_graph = None


_ORIG_MEMORY_STEP = None


def _memory_off(self, state):
    """L1：TradingAgents 内置记忆关闭。不结算旧决策（不调用反思 LLM），不向组合经理注入 past_context。"""
    return {"past_context": "", "memory_note": "交易台已关闭 TradingAgents 内置记忆注入（learning.ta_memory_inject=false）"}


def patch_ta_memory(cfg, cls):
    """L1：按 learning.ta_memory_inject 决定是否使用 TradingAgents 的 _memory_step。
    图在 __init__ 里绑定 self._memory_step，所以必须在实例化前改类属性；不改 vendor 源码。"""
    global _ORIG_MEMORY_STEP
    if _ORIG_MEMORY_STEP is None:
        _ORIG_MEMORY_STEP = cls._memory_step
    cls._memory_step = _ORIG_MEMORY_STEP if cfg["learning"].get("ta_memory_inject", False) else _memory_off
    return cls._memory_step


def build_graph(cfg, tracker):
    global _graph
    if _graph is not None:
        return _graph
    dllm.prepare_env(cfg)
    okx_data.install()
    roles.install(cfg, tracker)
    from desk import tools
    tools.install_market_tools()
    if os.environ.get("TA_DESK_MOCK_LLM") == "1":
        dllm.install_fake_into_tradingagents()
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    patch_ta_memory(cfg, TradingAgentsGraph)
    tc = dllm.ta_config(cfg)
    for k in ("results_dir", "data_cache_dir"):
        os.makedirs(tc[k], exist_ok=True)
    os.makedirs(os.path.dirname(tc["memory_log_path"]), exist_ok=True)
    _graph = TradingAgentsGraph(selected_analysts=tuple(cfg["analysts"]), config=tc, callbacks=[tracker])
    return _graph


def signed_qty(p: dict) -> float:
    """B3：持仓数量带方向（空为负）。优先看 pos_side，其次 side。"""
    q = abs(float(p.get("qty") or 0))
    sd = p.get("pos_side") if p.get("pos_side") in ("long", "short") else p.get("side")
    return -q if sd == "short" else q


def portfolio_ctx(book: dict):
    from tradingagents.portfolio import PortfolioContext, Position
    net: dict = {}
    for p in book["positions"]:
        n = net.setdefault(p["ticker"], {"q": 0.0, "cost": 0.0})
        q = signed_qty(p)
        n["q"] += q
        n["cost"] += abs(q) * float(p.get("avg_px") or 0)
    pos = [Position(ticker=t, quantity=v["q"], average_price=(v["cost"] / abs(v["q"])) if v["q"] else 0.0)
           for t, v in net.items()]
    return PortfolioContext(cash=round(book["cash"], 2), currency="USDT", positions=pos)


def position_brief(book: dict, inst_id: str) -> str:
    """B3：给每个角色的中文持仓说明（方向、张数带符号、均价、止损止盈）。"""
    L = []
    for p in book.get("positions", []):
        sd = "空" if signed_qty(p) < 0 else "多"
        ct = p.get("contracts")
        sct = (-1 if sd == "空" else 1) * float(ct) if ct is not None else None
        L.append(f"- {p.get('inst_id')}：{sd}单（posSide={p.get('pos_side') or p.get('side')}），"
                 f"数量 {signed_qty(p):+g}" + (f"，张数 {sct:+g}" if sct is not None else "")
                 + f"，均价 {p.get('avg_px')}，止损 {p.get('sl_px') or p.get('stop_loss') or '无'}，止盈 {p.get('tp_px') or p.get('take_profit') or '无'}"
                 + ("（本币）" if p.get("inst_id") == inst_id else ""))
    head = "## 当前模拟盘持仓（数量为负表示空单；空单价格下跌才盈利）\n"
    return head + ("\n".join(L) if L else "- 无持仓")


def x_feed(ccy: str, trade_date: str) -> str:
    """X（推特）讨论：盒子上没有用户凭据无法直接搜索 X；由 Grok Bot 例行任务预先写入
    state/x_feed/<日期>/<币>.md（可选）。没有则返回空，情绪分析师仍有 Reddit/StockTwits。"""
    from desk import sessions
    p = settings.STATE / "x_feed" / sessions.key(trade_date, settings.SESSION) / f"{ccy}.md"   # 晚场读 <日期>-evening
    return p.read_text(encoding="utf-8")[:6000] if p.exists() else ""


def x_feed_missing_alert(trade_date: str, ccy: str) -> bool:
    """L4：X 讨论摘录缺失时每天只告警一次（kind=x_feed_missing），标记文件 state/alerts/x_feed_missing_<日期>。"""
    from desk import sessions
    mark = settings.STATE / "alerts" / f"x_feed_missing_{sessions.key(trade_date, settings.SESSION)}"   # 每场一次
    if mark.exists():
        return False
    mark.parent.mkdir(parents=True, exist_ok=True)
    mark.write_text(ccy, encoding="utf-8")
    try:
        from desk import okx_demo
        okx_demo.log_event("x_feed_missing", date=trade_date, session=settings.SESSION, first_ccy=ccy)
        from desk.demo_ws import notify
        notify({"kind": "x_feed_missing", "date": sessions.key(trade_date, settings.SESSION), "first_ccy": ccy})
    except Exception as e:
        log.warning("x_feed_missing 告警失败：%s", e)
    return True


def analyze(cfg, coin: dict, trade_date: str, book: dict, tracker, extra_brief: str = "") -> dict:
    g = build_graph(cfg, tracker)
    from desk import flows, tools, untrusted
    tools._AS_OF["date"] = trade_date
    ccy = coin["inst_id"].split("-")[0]
    fl = flows.analyze(cfg, ccy, tracker) if "flows" in cfg.get("desk_analysts", ["flows"]) else {"report": "", "data": ""}
    xf = x_feed(ccy, trade_date)
    if not xf:
        x_feed_missing_alert(trade_date, ccy)
    briefs = {r: "## 资金流/衍生品分析师报告\n" + fl["report"] for r in roles.FLOWS_READERS if fl["report"]}
    briefs["sentiment"] = ("## X（推特）上的讨论摘录（由例行任务采集）\n" + untrusted.wrap("X 摘录", xf)) if xf else ""
    briefs["market"] = ("你额外拥有 run_backtest 工具：可以在 OKX 历史K线上回测你想到的做多条件（含样本外统计；做空可用反向条件理解），"
                        "用来检验你的技术判断是否有历史证据；样本少于 30 笔的结果不要当作证据。")
    try:   # FRED 宏观摘要只注入新闻分析师（按日缓存；失败降级为"不可用"，不影响分析）
        from desk import macro
        if macro.enabled(cfg):
            briefs["news"] = macro.brief(macro.safe_get(cfg, trade_date))
    except Exception as e:
        log.warning("宏观摘要失败：%s", e)
    briefs["*"] = position_brief(book, coin["inst_id"])   # B3：所有角色都看到带方向的持仓
    if extra_brief:   # 再讨论触发原因（止损/止盈）等共享上下文
        briefs["*"] = ((briefs["*"] or "") + "\n\n" + extra_brief).strip()
    roles.set_briefs(briefs)
    tok = dllm.STAGE.set(f"{coin["inst_id"]}/分析图")
    try:
        fs, signal = g.propagate(coin["ticker"], trade_date, asset_type="crypto", portfolio=portfolio_ctx(book))
    finally:
        dllm.STAGE.reset(tok)
        roles.set_briefs({})
    fs["_flows_report"] = fl["report"]
    fs["_x_feed"] = bool(xf)
    fs["_flows_missing"] = bool(fl.get("data_missing"))
    try:
        path = g.save_reports(fs, coin["ticker"], html=False)
    except Exception as e:  # 报告树写入失败不影响交易流程
        log.warning("save_reports 失败：%s", e)
        path = None
    return {"final_state": fs, "signal": signal, "report_dir": str(path) if path else None}
