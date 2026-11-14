"""提示词体积回归（2026-11-14 用量事故）：锁定全仓提示词组装的体积上限，防无上限组件再次混入。

事故背景：2026-11-14 晚场用量台账出现单次 opus 调用输入 18.6–32 万 token 的记录。逐组件实测
（mock + 生产量级长报告）证明：TM 提示词与图内下游节点提示词的最大合法体积只有约 4.5 万字符，
膨胀只可能来自无上限的原始载荷通道——分析师工具结果（已套上限，desk/tools.py）与任何未来的
上下文累积。本文件的断言就是那次实测的上限：任何组件把单币 TM 提示词或图内关键节点提示词
撑过上限，测试立即失败。另外锁定调用咽喉的保险丝（desk/llm_retry.guard_input）行为。
"""
import pytest

from desk import settings

TM_TOTAL_CAP = 60_000        # TM 提示词 + 下单工具说明的字符上限（实测合法值约 2 万）
NODE_PROMPT_CAP = 150_000    # 图内单个节点单次调用输入字符上限（实测合法最大约 4.5 万）


@pytest.fixture(scope="module")
def cfg():
    return settings.load_config()


# ---------------------------------------------------------------- 工具输出上限
def test_tool_output_cap_unit():
    from langchain_core.tools import tool

    from desk.tools import TOOL_OUTPUT_CAP, cap_tool_output

    @tool
    def big_tool(symbol: str) -> str:
        """返回超长结果的工具"""
        return "x" * 100_000

    @tool
    def small_tool(symbol: str) -> str:
        """返回短结果的工具"""
        return "short"

    w = cap_tool_output(big_tool)
    out = w.invoke({"symbol": "BTC-USD"})
    assert len(out) <= TOOL_OUTPUT_CAP + 200 and "已截断" in out
    assert w.name == big_tool.name and w.args_schema is big_tool.args_schema
    assert cap_tool_output(w) is w                      # 幂等
    assert cap_tool_output(small_tool).invoke({"symbol": "BTC-USD"}) == "short"   # 短结果原样


def test_install_tool_caps_covers_all_analysts():
    from desk import tools
    tools.install_market_tools()
    tools.install_market_tools()                        # 幂等：再装一次不重复包裹/追加
    import tradingagents.graph.analyst_execution as ae
    from tradingagents.agents.analysts import fundamentals_analyst as fa
    from tradingagents.agents.analysts import market_analyst as ma
    from tradingagents.agents.analysts import news_analyst as na
    for mod in (ma, na, fa):
        assert all(getattr(t, "_ta_desk_capped", False) for t in mod.TOOLS)
    for key in ("market", "news", "fundamentals"):
        assert all(getattr(t, "_ta_desk_capped", False) for t in ae.ANALYST_NODE_SPECS[key].tools)
    assert [t.name for t in ma.TOOLS].count("run_backtest") == 1


# ---------------------------------------------------------------- TM 提示词上限
def test_tm_prompt_bounded_with_fat_texts(cfg):
    from desk import manager, regime_judge, tm_order_tool
    from desk.roles import ROLE_KEYS

    fat = "填" * 30_000     # 每个角色文本都给到远超生产量级（生产 complete_report 全场约 6 万字符）
    texts = {k: fat for k in ("market", "sentiment", "news", "flows", "bull", "bear",
                              "research_manager", "trader", "aggressive", "conservative",
                              "neutral", "portfolio_manager")}
    st = {r: {"direction": "bearish", "confidence": 0.6, "target": 60000.0, "stop": 72000.0,
              "horizon_days": 5, "reason": "理" * 200} for r in ROLE_KEYS}
    mk = {"last": 67000.0, "bar_date": "2026-11-13", "close": 67000.0, "ret_5d": -11.0,
          "ret_20d": -16.0, "atr14": 2400.0, "hi_20": 76000.0, "lo_20": 66400.0}
    bk = {"equity": 106000.0, "cash": 100000.0, "exposure": 0.0}
    feat = {"bars": 260, "rsi14": 22.0, "pos_in_range": 0.06, "vol_z_last": 1.9, "vol_z_max5": 1.9,
            "last_chg_atr": -1.25, "flow_missing": False, "funding_now": 0.00004,
            "funding_pctile_90d": 34.7, "funding_hist_n": 288, "oi_usd": 2.27e9, "oi_chg_5d_pct": -9.67}
    for w_ in (20, 50, 200):
        feat[f"ma{w_}"] = 70000.0
        feat[f"ma{w_}_slope_pct"] = -3.0
        feat[f"ma{w_}_slope_pct_prev5"] = -1.6
        feat[f"dev_ma{w_}_pct"] = -10.0
    judge = regime_judge.normalize_judgment(
        {"direction": "short", "strength": "strong", "stage": "mid", "confidence": 0.8,
         "evidence": ["MA20 斜率 -3.0%"], "summary": "空头强趋势中段", "exhaustion_signals": []})
    regime = regime_judge.build_regime(cfg, judge, feat)
    coin = {"name": "比特币", "inst_id": "BTC-USDT-SWAP"}
    w = manager.weights(cfg)
    prompt = manager._prompt(cfg, coin, mk, None, bk, st, w, -0.45, texts, None, regime)
    tctx = manager.tool_context(cfg, coin["inst_id"], mk, bk, None, -0.45, regime)
    total = len(prompt) + len(tm_order_tool.instructions(tctx, cfg))
    assert total <= TM_TOTAL_CAP, f"TM 提示词总体积 {total} 超上限 {TM_TOTAL_CAP}"


# ---------------------------------------------------------------- 调用咽喉保险丝
def test_prompt_guard():
    from langchain_core.messages import HumanMessage, SystemMessage

    from desk import llm_retry
    llm_retry.TRUNCATIONS.clear()
    try:
        small = [SystemMessage(content="sys"), HumanMessage(content="hello")]
        out, cut = llm_retry.guard_input(small, cap=100_000)
        assert out is small and cut == 0                       # 未超限：原样返回，不留痕
        assert not llm_retry.TRUNCATIONS

        big = [SystemMessage(content="sys" * 100), HumanMessage(content="y" * 500_000)]
        out, cut = llm_retry.guard_input(big, cap=100_000)
        total = sum(llm_retry._content_len(m.content) for m in out)
        assert cut > 0 and total <= 100_000
        assert len(out) == 2 and isinstance(out[0], SystemMessage)   # 只截内容，不删消息
        assert out[0].content == "sys" * 100                          # 系统消息未被动
        assert "截断中段" in out[1].content
        assert len(llm_retry.TRUNCATIONS) == 1

        out, cut = llm_retry.guard_input("z" * 300_000, cap=100_000)  # 纯字符串入参同样受控
        assert cut > 0 and len(out) <= 100_000
    finally:
        llm_retry.TRUNCATIONS.clear()


# ---------------------------------------------------------------- 图内节点提示词上限
_FAT_REPORT = ("## 分析报告\n" + ("市场处于下跌趋势，价格贴近区间下沿，动量指标超卖但未见背离，"
               "成交量温和放大，资金面空头拥挤。证据：均线空头排列，RSI 处于低位，斜率仍在加深。\n" * 130)
               + "**Rating**: Sell\nFINAL TRANSACTION PROPOSAL: **SELL**\n"
               "【立场】方向=看空；目标价=60000；止损价=72000；置信度=0.6；周期=5天")


def test_graph_node_prompts_bounded(cfg, monkeypatch):
    """mock LLM 产出生产量级长报告（每份约 1.2 万字符）跑完整单币分析，逐次记录实际组装的
    prompt 体积：任何节点（尤其 opus 席位 research_manager / conservative）不得超上限。"""
    from desk import analysis, llm as dllm
    from desk.llm import FakeChatModel

    records = []
    orig = FakeChatModel._generate

    def spy(self, messages, stop=None, run_manager=None, **kw):
        records.append((self.tier, sum(len(m.content) if isinstance(m.content, str)
                                       else len(str(m.content)) for m in messages)))
        res = orig(self, messages, stop=stop, run_manager=run_manager, **kw)
        msg = res.generations[0].message
        if not msg.tool_calls and isinstance(msg.content, str) and len(msg.content) > 10:
            msg.content = _FAT_REPORT
        return res

    monkeypatch.setattr(FakeChatModel, "_generate", spy)
    tracker = dllm.UsageTracker(cfg)
    coin = {"name": "比特币", "inst_id": "BTC-USDT-SWAP", "ticker": "BTC-USD"}
    book = {"positions": [], "cash": 100000.0, "equity": 100000.0, "exposure": 0.0}
    analysis.analyze(cfg, coin, "2026-11-13", book, tracker)
    assert records, "应至少记录到一次图内调用"
    worst = max(c for _, c in records)
    assert worst >= 20_000, f"长报告未真正流入下游（最大 prompt 仅 {worst}），测试失去意义"
    for tier, chars in records:
        assert chars <= NODE_PROMPT_CAP, f"节点 {tier} 的 prompt {chars} 字符超上限 {NODE_PROMPT_CAP}"
