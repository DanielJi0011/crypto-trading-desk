"""调用 TradingAgents（v0.7.0）对单个币种做一轮完整的多角色分析。"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime

from desk import llm as dllm
from desk import okx_data, roles, settings

log = logging.getLogger("ta-desk.analysis")
_graph = None
_GRAPH_LOCK = threading.RLock()     # 并行分析：建图/打补丁只做一次
_XFEED_LOCK = threading.Lock()     # 并行分析：x_feed 缺失告警“检查标记→写标记”原子化，每场只告警一次


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


def _beijing_today() -> str:
    """北京时间的今天（YYYY-MM-DD）。台子的交易日期一律是北京日期，
    校验时钟必须与之同口径（服务器时钟是 UTC）。"""
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")


def _validate_trade_date_bj(trade_date) -> str:
    """与 vendor _validate_trade_date 同口径，但“今天”按北京时间算。
    服务器时钟是 UTC：07:35 北京场启动时是 23:35 UTC 前一日，按 UTC
    算“今天”会把当日误判为未来日期（2026-11-15 早场实测：三币全在
    propagate 里抛 ValueError: trade_date cannot be in the future）。
    旧 08:05 北京场（= 00:05 UTC 当日）刚好跨过 UTC 午夜所以没事；
    晚场 21:00 北京（= 13:00 UTC 当日）不受影响。"""
    value = str(trade_date)
    try:
        canonical = datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d") == value
    except ValueError:
        canonical = False
    if not canonical:
        raise ValueError(f"trade_date must be a date in YYYY-MM-DD format, got {trade_date!r}")
    if value > _beijing_today():
        raise ValueError(f"trade_date cannot be in the future: {value}")
    return value


def patch_trade_date(cfg, cls):
    """把 TradingAgents 的 _validate_trade_date 换成北京时间口径。
    不改 vendor 源码：只替换 trading_graph 模块属性（propagate 通过模块
    全局名调用它），类实例化前打补丁，与 patch_ta_memory 同一位置。
    补丁自带正确时区，不依赖 TZ 环境变量。"""
    import tradingagents.graph.trading_graph as _tg
    _tg._validate_trade_date = _validate_trade_date_bj
    return _validate_trade_date_bj


def build_graph(cfg, tracker):
    with _GRAPH_LOCK:
        return _build_graph(cfg, tracker)


def _build_graph(cfg, tracker):
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
    patch_trade_date(cfg, TradingAgentsGraph)
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
    """B3：给每个角色的中文持仓说明（方向、张数带符号、均价、止损止盈、浮盈亏）。全员共享，含 upnl。"""
    L = []
    for p in book.get("positions", []):
        sd = "空" if signed_qty(p) < 0 else "多"
        ct = p.get("contracts")
        sct = (-1 if sd == "空" else 1) * float(ct) if ct is not None else None
        upnl = p.get("upnl")
        upnl_s = f"，浮盈亏 {float(upnl):+.2f} USDT" if upnl is not None else ""
        L.append(f"- {p.get('inst_id')}：{sd}单（posSide={p.get('pos_side') or p.get('side')}），"
                 f"数量 {signed_qty(p):+g}" + (f"，张数 {sct:+g}" if sct is not None else "")
                 + f"，均价 {p.get('avg_px')}，止损 {p.get('sl_px') or p.get('stop_loss') or '无'}，止盈 {p.get('tp_px') or p.get('take_profit') or '无'}"
                 + upnl_s
                 + ("（本币）" if p.get("inst_id") == inst_id else ""))
    head = "## 当前模拟盘持仓（数量为负表示空单；空单价格下跌才盈利）\n"
    return head + ("\n".join(L) if L else "- 无持仓")


PENDING_HEAD = "## 当前挂着的条件单"
PENDING_NOTE = ("（以下是已挂出、尚未成交的条件入场单。正文不要重复论证相同价位，但如果你的方向与挂单同向，"
                "【立场】行必须写「对现有挂单=保留/改价/撤销/改现价」：保留=支持按该挂单价位入场（可照抄其触发价作入场价）；"
                "改价=按你写的入场价和入场条件改挂；撤销=不支持该挂单；改现价=改为现价开仓。"
                "没写入场价也没写该字段的同向角色，程序按「保留」处理。）")


def _fmt_trigger(t: dict, meta: dict) -> str:
    inst = t.get("instId") or t.get("inst_id") or meta.get("inst_id")
    side = meta.get("side") or t.get("side")
    if side in ("buy", "sell"):
        ps = t.get("posSide")
        side = ps if ps in ("long", "short") else ("long" if side == "buy" else "short")
    sd = {"long": "做多", "short": "做空"}.get(side, str(side or "?"))
    px = t.get("triggerPx") or t.get("trigger_px") or meta.get("trigger_px")
    sz = t.get("sz") or meta.get("sz") or t.get("contracts")
    sl = tp = None
    for a in t.get("attachAlgoOrds") or []:
        sl = sl or a.get("slTriggerPx"); tp = tp or a.get("tpTriggerPx")
    sl = sl or t.get("slTriggerPx") or t.get("stop_loss") or meta.get("stop_loss")
    tp = tp or t.get("tpTriggerPx") or t.get("take_profit") or meta.get("take_profit")
    exp = t.get("expires") or meta.get("expires")
    return (f"- {inst}：{sd}，触发价 {px}" + (f"，张数 {sz}" if sz not in (None, "") else "")
            + f"，止损 {sl or '无'}，止盈 {tp or '无'}，到期 {exp or '未知'}")


def pending_brief(book: dict) -> str:
    """当前挂着的条件入场单（只读信息）。来源：模拟盘 book["triggers"]（= OKX orders-algo-pending ordType=trigger，
    由 demo_exec.book 拉取），用登记表 state/ledger/demo_orders.json 的 triggers[algoId] 补到期等元数据；
    纸面模式 book 无 triggers 键时读 state/ledger/pending_entries.json。任何失败降级为"挂单信息不可用"。"""
    try:
        if "triggers" in book:
            from desk import demo_exec
            try:
                regt = demo_exec.reg().get("triggers", {})
            except Exception:
                regt = {}
            items = [(t, regt.get(str(t.get("algoId")), {})) for t in (book.get("triggers") or [])]
        else:
            from desk import pending
            items = [({**o, "inst_id": o.get("inst_id") or k}, {}) for k, o in (pending.load() or {}).items()
                     if isinstance(o, dict) and o.get("status", "pending") == "pending"]
        L = [_fmt_trigger(t, m) for t, m in items]
        return PENDING_HEAD + PENDING_NOTE + "\n" + ("\n".join(L) if L else "- 无")
    except Exception as e:
        log.warning("读取挂单失败：%s", type(e).__name__)
        return PENDING_HEAD + "\n- 挂单信息不可用"


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
    with _XFEED_LOCK:
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


def _run_signature(g, pf):
    fn = getattr(g, "_run_signature", None)
    return fn("crypto", pf) if fn else None


def _prepare_checkpoint(g, coin: dict, trade_date: str, pf, resume: bool):
    """席位级续跑的存档点准备（仅存档点启用时调用，语义见 desk/resume.py）。

    返回 (本次 propagate 应传入的组合上下文, 是否将从存档点续跑)。
    - resume 且本场次 run-ctx 旁车在、且该线程确有存档步数：喂回崩溃那次运行的
      原始组合（签名含组合指纹，现读账本几乎必然对不上），vendor 在 propagate
      里自动从最后完成节点续跑。
    - 其余情况（fresh 语义：定时场次、整场重跑、续跑但无存档可复用）：先清掉本次
      将用到的线程与旁车记录的上次线程，保证从干净状态开始，再写下本次 run-ctx。
      图成功收尾时 vendor 会自行清存档点（_run_graph 的 clear_checkpoint_on_success）。
    """
    from desk import resume as _resume
    from tradingagents.graph.checkpointer import checkpoint_step
    ticker = coin["ticker"]
    ctx = _resume.read_run_ctx(ticker, trade_date, settings.SESSION)
    if resume and ctx and ctx.get("portfolio"):
        try:
            from tradingagents.portfolio import PortfolioContext
            pf0 = PortfolioContext.model_validate(ctx["portfolio"])
            step = checkpoint_step(g.config["data_cache_dir"], ticker, str(trade_date),
                                   _run_signature(g, pf0) or "")
        except Exception as e:
            log.warning("席位级续跑预检失败（按整币新跑处理）：%s", e)
            pf0, step = None, None
        if pf0 is not None and step is not None:
            log.info("席位级续跑：%s %s 从存档点第 %s 步续跑", ticker, trade_date, step)
            return pf0, True
        log.info("席位级续跑：%s %s 无可复用存档点，整币新跑", ticker, trade_date)
    try:
        g.clear_checkpoint_on_success(ticker, trade_date, "crypto", pf)
    except Exception as e:
        log.warning("清存档点失败（现读组合）：%s", e)
    if ctx and ctx.get("portfolio"):
        try:
            from tradingagents.portfolio import PortfolioContext
            pf_old = PortfolioContext.model_validate(ctx["portfolio"])
            if pf_old.fingerprint() != pf.fingerprint():
                g.clear_checkpoint_on_success(ticker, trade_date, "crypto", pf_old)
        except Exception as e:
            log.warning("清存档点失败（上次组合）：%s", e)
    try:
        _resume.write_run_ctx(ticker, trade_date, settings.SESSION, pf, _run_signature(g, pf))
    except Exception as e:
        log.warning("run-ctx 旁车写入失败：%s", e)
    return pf, False


def analyze(cfg, coin: dict, trade_date: str, book: dict, tracker, extra_brief: str = "", resume: bool = False) -> dict:
    g = build_graph(cfg, tracker)
    from desk import flows, tools, untrusted
    tools._AS_OF["date"] = trade_date
    ccy = coin["inst_id"].split("-")[0]
    from desk import sessions as _sess
    shared = position_brief(book, coin["inst_id"]) + "\n\n" + pending_brief(book) + "\n\n" + _sess.CADENCE_NOTE   # 所有角色共享：持仓 + 已挂条件单 + 固定时间决策说明（2026-11-05 用户要求）
    if extra_brief:
        shared = (shared + "\n\n" + extra_brief).strip()
    roles.set_briefs({"*": shared})   # 资金流分析师在图外先运行，也要看到共享信息
    try:
        fl = flows.analyze(cfg, ccy, tracker) if "flows" in cfg.get("desk_analysts", ["flows"]) else {"report": "", "data": ""}
    finally:
        roles.set_briefs({})
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
    briefs["*"] = shared   # B3 持仓 + 当前挂着的条件单 + 再讨论触发原因（止损/止盈），全体角色共享
    roles.set_briefs(briefs)
    pf = portfolio_ctx(book)
    resumed = False
    if dllm.checkpoint_enabled(cfg):
        pf, resumed = _prepare_checkpoint(g, coin, trade_date, pf, resume)
    tok = dllm.STAGE.set(f"{coin["inst_id"]}/分析图")
    try:
        fs, signal = g.propagate(coin["ticker"], trade_date, asset_type="crypto", portfolio=pf)
    finally:
        dllm.STAGE.reset(tok)
        roles.set_briefs({})
    fs["_flows_report"] = fl["report"]
    fs["_x_feed"] = bool(xf)
    fs["_flows_missing"] = bool(fl.get("data_missing"))
    try:   # 尾段续跑旁车：图外字段随图结果一并落盘（无条件写，失败降级），见 desk/resume.py
        from desk import resume as _resume
        _resume.write_tail_sidecar(coin["ticker"], trade_date, settings.SESSION, fs, signal)
    except Exception as e:
        log.warning("尾段旁车写入失败：%s", e)
    try:
        path = g.save_reports(fs, coin["ticker"], html=False)
    except Exception as e:  # 报告树写入失败不影响交易流程
        log.warning("save_reports 失败：%s", e)
        path = None
    return {"final_state": fs, "signal": signal, "report_dir": str(path) if path else None,
            "resumed_from_checkpoint": resumed}
