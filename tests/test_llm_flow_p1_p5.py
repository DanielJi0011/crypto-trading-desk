"""P1–P5（2026-11-15 LLM 数据流审查 + 用户准确性硬约束/P3 修订口径）。

覆盖：P1 工具循环轮数与分工具上限、截断留痕、get_stock_data 不再拼 4H/1H 参考块；
P2 风险三人组摘要（完整性校验→回退原文+事件留痕）、研究员提示词去重；
P3 立场行×结构化交叉校验（自由文本为主源、冲突只落标记、绝不拒绝）、双语容错解析；
P4 立场行说明是指引式英文；P5 交易经理提示词行情去重。
"""
import pandas as pd
import pytest

from desk import digest, llm as dllm, manager, okx_data, regime_judge as RJ, roles, settings, stances, tools


# ---------------- P1：工具循环纪律 ----------------

def test_default_tool_rounds_is_4():
    cfg = settings.load_config()
    cfg = {**cfg, "llm": {k: v for k, v in cfg["llm"].items() if k != "max_tool_rounds"}}
    assert dllm.ta_config(cfg)["max_tool_rounds"] == 4


def test_market_analyst_keeps_rounds_exception(monkeypatch):
    import tradingagents.graph.setup as gsetup
    seen = {}

    class Spec:
        def __init__(self, key):
            self.key = key

    def recorder(spec, agent, rounds):
        seen[spec.key] = rounds
        return None

    monkeypatch.setattr(gsetup, "_analyst_graph", recorder)
    monkeypatch.setattr(tools, "_ANALYST_GRAPH_ORIG", None)
    tools.install_tool_loop_discipline({"llm": {}})
    gsetup._analyst_graph(Spec("market"), None, 4)
    gsetup._analyst_graph(Spec("news"), None, 4)
    assert seen == {"market": 8, "news": 4}          # 市场（回测路径）保留 8，其余按全局 4
    tools.install_tool_loop_discipline({"llm": {}})   # 幂等：不再叠包裹
    gsetup._analyst_graph(Spec("market"), None, 4)
    assert seen["market"] == 8


def test_per_tool_caps_and_truncation_registry():
    from langchain_core.tools import tool as lc_tool

    @lc_tool
    def get_stock_data(symbol: str) -> str:
        """K 线"""
        return "x" * 30_000

    @lc_tool
    def other_tool(symbol: str) -> str:
        """其它"""
        return "y" * 30_000

    assert tools.cap_for("get_stock_data") == 12_000 and tools.cap_for("other_tool") == 24_000
    tools.TOOL_TRUNCATIONS.clear()
    out = tools.cap_tool_output(get_stock_data).invoke({"symbol": "BTC"})
    assert len(out) < 13_000 and "[tool output truncated" in out
    out2 = tools.cap_tool_output(other_tool).invoke({"symbol": "BTC"})
    assert 24_000 < len(out2) < 25_000
    assert [t["tool"] for t in tools.TOOL_TRUNCATIONS] == ["get_stock_data", "other_tool"]
    tools.TOOL_TRUNCATIONS.clear()


def test_stock_data_tool_result_has_no_intraday_block(monkeypatch):
    n = 30
    df = pd.DataFrame({"Date": pd.date_range("2026-10-01", periods=n, freq="D"),
                       "Open": [100.0] * n, "High": [101.0] * n, "Low": [99.0] * n,
                       "Close": [100.5] * n, "Volume": [1.0] * n, "VolumeUSDT": [2.0] * n})
    monkeypatch.setattr(okx_data, "candles", lambda inst, bar="1Dutc", days=420: df)
    out = okx_data.okx_get_stock_data("BTC-USD", "2026-10-01", "2026-10-30")
    assert "# 4H" not in out and "# 1H" not in out and "Total records: 30" in out


def test_truncation_lands_on_stance_data_note():
    fs = {"_tool_truncations": [{"tool": "get_stock_data", "chars": 30000, "cap": 12000, "stage": "x"}]}
    cfg = settings.load_config()
    out = stances.extract(fs, cfg)
    assert "tool output truncated: get_stock_data" in (out["market"].get("data_note") or "")
    assert out["market"]["status"] not in ("data_missing",)   # 截断≠数据缺失，状态口径不变


# ---------------- P2：风险三人组摘要 ----------------

MKT = ("Technical view on BTC. ATR14 currently 812.5.\n"
       "Filler analysis prose that only the full corpus carries. FULLCORPUSMARKET\n"
       "Key support at 60000, resistance at 72000 overhead.\n"
       "【立场】方向=看空；目标价=60000；止损价=72000；置信度=0.62；周期=5天；入场价=69000；入场条件=涨到")
SEN = ("**Overall Sentiment:** **Bearish** (Score: 3.5/10)\n**Confidence:** Medium\n"
       "Crowd is fearful and positioning defensively here.\n"
       "More colour that only the full corpus carries. FULLCORPUSSENT\n"
       "【立场】方向=看空；目标价=无；止损价=无；置信度=0.55；周期=3天")
NEWS = ("Headlines stay negative for crypto risk assets today.\n"
        "Background detail that only the full corpus carries. FULLCORPUSNEWS\n"
        "【立场】方向=看空；目标价=无；止损价=无；置信度=0.5；周期=3天")
FDATA = ("## BTC derivatives & flows data\n- funding_now (8h): +0.0100% (annualized approx 10.9%)\n"
         "- OKX perpetual open interest (oi_usd): 12.345 billion USD")
FREPORT = "资金面偏空，杠杆多头拥挤，去杠杆风险上升。\n【立场】方向=看空；目标价=无；止损价=无；置信度=0.6；周期=3天"
REPORTS = {"market": MKT, "sentiment": SEN, "news": NEWS}


def test_digest_complete_keeps_every_required_value():
    b = digest.build_digest(REPORTS, FREPORT, FDATA)
    assert b["missing"] == []
    for v in ("60000", "72000", "69000", "812.5", "+0.0100", "0.62", "0.55", "0.50", "12.345"):
        assert v in b["text"], v
    assert len(b["text"]) < 2_600


def test_digest_news_conditional_entry_does_not_fall_back():
    # 回归：2026-11-16 早场 6 次 digest_incomplete_fallback 全缺 news.entry_px——
    # 新闻分析师的立场行带条件式入场价（入场价+入场条件），旧摘要只写了 tgt/stop，
    # 完整性自检报缺失后整场回退全文，P2 省 token 当场归零。
    news_entry = NEWS + "；入场价=1860；入场条件=涨到"
    b = digest.build_digest({**REPORTS, "news": news_entry}, FREPORT, FDATA)
    assert b["missing"] == []
    assert "1860" in b["text"] and "(gte)" in b["slots"]["news"]
    assert len(b["text"]) < 2_600


def test_digest_missing_field_falls_back_with_event(monkeypatch):
    broken = {**REPORTS, "market": "a market report with no stance line and no numbers"}
    assert digest.build_digest(broken, FREPORT, FDATA)["missing"] == ["market.stance"]
    digest.EVENTS.clear()
    monkeypatch.setattr(settings, "append_jsonl", lambda *a, **k: None)
    seen = {}

    def node(state):
        seen["state"] = state
        return {"ok": True}

    state = {"market_report": broken["market"], "sentiment_report": SEN, "news_report": NEWS, "fundamentals_report": ""}
    assert digest.run_with_digest(node, state) == {"ok": True}
    assert seen["state"] is state                                   # 回退：原节点拿到的是原文状态
    assert digest.EVENTS and digest.EVENTS[-1]["event"] == "digest_incomplete_fallback"
    assert digest.EVENTS[-1]["missing"] == ["market.stance"]
    digest.EVENTS.clear()


def test_digest_path_replaces_reports_when_complete():
    seen = {}

    def node(state):
        seen["state"] = state
        return {"ok": True}

    state = {"market_report": MKT, "sentiment_report": SEN, "news_report": NEWS, "fundamentals_report": ""}
    assert digest.run_with_digest(node, state) == {"ok": True}
    s2 = seen["state"]
    assert "RISK DIGEST" in s2["market_report"] and "FULLCORPUSMARKET" not in s2["market_report"]
    assert "FULLCORPUSSENT" not in s2["sentiment_report"] and "FULLCORPUSNEWS" not in s2["news_report"]


def test_trio_factory_routes_through_digest():
    import tradingagents.graph.setup as setup
    roles.install(None, None)
    captured = {}

    class Stub:
        def invoke(self, msgs, config=None, **kw):
            captured["msgs"] = msgs

            class R:
                content = "aggressive view"
            return R()

    node = setup.create_aggressive_debator(Stub())
    state = {"market_report": MKT, "sentiment_report": SEN, "news_report": NEWS, "fundamentals_report": "",
             "trader_investment_plan": "plan", "company_of_interest": "BTC-USD",
             "risk_debate_state": {"history": "", "aggressive_history": "", "count": 0,
                                   "current_conservative_response": "", "current_neutral_response": ""}}
    node(state)
    text = str(captured["msgs"])
    assert "RISK DIGEST" in text and "FULLCORPUSMARKET" not in text


def test_researcher_prompt_dedupes_last_argument():
    arg = "Bear Analyst: shorts are in control here"
    prompt = (f"Conversation history of the debate: Bull Analyst: hi\n{arg}\n"
              f"Last bear argument: {arg}\nUse this information to deliver a compelling bull argument.")
    out = roles._dedupe_last_argument(prompt)
    assert out.count("shorts are in control") == 1 and "identical to the last entry" in out
    other = prompt.replace("Last bear argument: Bear Analyst: shorts", "Last bear argument: Bear Analyst: longs")
    assert roles._dedupe_last_argument(other) == other              # 对不上就不动


# ---------------- P3：交叉校验（自由文本为主源） ----------------

def test_bilingual_stance_line_parses_identically():
    en = stances.parse_stance("text\n[STANCE] dir=short; tgt=60000; stop=72000; conf=0.6; hz=5; entry=69000; cond=gte; pending=keep")
    zh = stances.parse_stance("text\n【立场】方向=看空；目标价=60000；止损价=72000；置信度=0.6；周期=5天；入场价=69000；入场条件=涨到；对现有挂单=保留")
    for k in ("direction", "target", "stop", "confidence", "horizon_days", "entry_px", "entry_condition", "pending_action"):
        assert en[k] == zh[k], k


def test_cross_check_conflict_complete_prose_wins_and_flags():
    prose = stances.parse_stance("x\n【立场】方向=看多；目标价=100；止损价=90；置信度=0.6；周期=5天")
    out = stances.cross_check("trader", prose, {"direction": "bearish", "confidence": 0.55, "source": "结构化"})
    assert out["direction"] == "bullish" and "-> prose" in out["cross_check"]


def test_cross_check_partial_prose_uses_structured_and_flags():
    prose = stances.parse_stance("x\n【立场】方向=看多；置信度=0.6")
    assert prose["partial"]
    out = stances.cross_check("trader", prose, {"direction": "bearish", "confidence": 0.55, "target": 88.0, "source": "结构化"})
    assert out["direction"] == "bearish" and out["target"] == 88.0 and "structured" in out["cross_check"]


def test_cross_check_prose_missing_uses_structured_with_flag():
    out = stances.cross_check("trader", None, {"direction": "bearish", "confidence": 0.55, "source": "结构化"})
    assert out["direction"] == "bearish" and out["cross_check"].startswith("prose_missing")


def test_cross_check_agreement_is_silent():
    prose = stances.parse_stance("x\n【立场】方向=看多；目标价=100；止损价=90；置信度=0.6；周期=5天")
    out = stances.cross_check("trader", prose, {"direction": "bullish", "confidence": 0.6, "source": "结构化"})
    assert "cross_check" not in out


def test_health_surfaces_cross_check_conflicts():
    dec = [{"inst_id": "BTC-USDT-SWAP", "stances": {
        "bull": {"direction": "bullish", "confidence": 0.6, "source": "立场行", "cross_check": "direction: prose bullish vs structured bearish -> prose"}}}]
    h = stances.health(dec, {"bull": 1.0})
    assert h["conflicts"] and h["conflicts"][0]["role"] == "bull"


# ---------------- P4 / P5 ----------------

def test_stance_spec_is_guidance_not_gate():
    assert "[STANCE]" in roles.STANCE_SPEC and "counted as neutral" in roles.STANCE_SPEC
    fixed = roles.refresh_fixed("## 输出要求（固定，不参与学习修订）\n- 旧规格\n\n## 经验规则\n- x")
    assert "[STANCE]" in fixed and "## 经验规则" in fixed            # 固定段被换新、手册正文不动


def _tm_prompt(regime):
    cfg = settings.load_config()
    mk = {"last": 70000.0, "close": 69500.0, "bar_date": "2026-11-14", "ret_5d": -3.0, "ret_20d": -8.0,
          "atr14": 1500.0, "hi_20": 76000.0, "lo_20": 64000.0}
    bk = {"equity": 100000.0, "cash": 90000.0, "exposure": 0.0, "positions": [], "triggers": []}
    st = {r: {"direction": "bearish", "confidence": 0.5, "target": None, "stop": None, "horizon_days": 5, "reason": "r"}
          for r in manager.ROLE_KEYS}
    return manager._prompt(cfg, cfg["coins"][0], mk, None, bk, st, {}, -0.5,
                           {"portfolio_manager": "pm", "trader": "t"}, None, regime)


def _regime(cfg):
    feat = {"bars": 260, "ma20": 71000.0, "ma20_slope_pct": -1.0, "dev_ma20_pct": -1.4,
            "ma50": 73000.0, "ma50_slope_pct": -1.2, "dev_ma50_pct": -4.1,
            "ma200": 80000.0, "ma200_slope_pct": 0.3, "dev_ma200_pct": -12.5,
            "rsi14": 30.0, "pos_in_range": 0.1, "weekly_direction": "short", "weekly_weeks": 40,
            "weekly_close": 69500.0, "weekly_ma10": 72000.0, "weekly_ma20": 75000.0, "weekly_ma20_slope_pct": -2.0,
            "h4_swing_high": 71500.0, "h4_swing_low": 66000.0, "funding_now": 0.0001, "funding_pctile_90d": 30.0,
            "funding_hist_n": 270, "oi_usd": 1.2e10, "oi_chg_5d_pct": -5.0}
    judge = {"direction": "short", "strength": "strong", "stage": "mid", "confidence": 0.7,
             "summary": "s", "evidence": ["e"]}
    return {"cfg": RJ.trend_cfg(cfg), "judge": judge, "features": feat, "active": True, "failed": False,
            "downgrade": False, "weekly": {"enabled": False}, "h4": {"enabled": False}}


def test_tm_prompt_dedupes_market_facts_when_features_present():
    cfg = settings.load_config()
    p = _tm_prompt(_regime(cfg))
    assert p.count("76000") == 1                                    # 20 日高只在趋势特征块出现一次
    assert "latest daily bar 2026-11-14 closed 69500" in p           # 特征块没有的两项仍在行情行
    assert "ATR14 = 2.14% of spot" in p


def test_tm_prompt_full_market_line_without_features():
    p = _tm_prompt(None)
    assert "20d high 76000 low 64000" in p and "last 70000" in p
