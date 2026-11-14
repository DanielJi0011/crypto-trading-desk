"""技术分析师专属工具：在 OKX 历史K线上跑 Python 回测（复用 /workspace/ai-trader/backtest.py）。"""
from __future__ import annotations

import json

import pandas as pd
from langchain_core.tools import tool

from desk import okx_data, settings

_AS_OF = {"date": None}


def ai_backtest():
    import sys
    if str(settings.AI_TRADER) not in sys.path:
        sys.path.insert(0, str(settings.AI_TRADER))
    import backtest
    return backtest


def history(inst: str, bar: str = "1Dutc", n: int = 1500) -> pd.DataFrame:
    d = okx_data.candles(inst, bar, n)
    if _AS_OF["date"] and bar.startswith("1D"):
        d = d[d.Date < pd.Timestamp(_AS_OF["date"])]        # 只用分析日之前已收盘的数据，防止未来函数
    return pd.DataFrame({"time": d.Date, "o": d.Open.astype(float), "h": d.High.astype(float), "l": d.Low.astype(float),
                         "c": d.Close.astype(float), "v": d.Volume.astype(float)}).reset_index(drop=True)


@tool
def run_backtest(symbol: str, entry_expr: str, sl_pct: float = 5.0, tp_pct: float | None = None,
                 max_bars: int = 5, bar: str = "1D") -> str:
    """在 OKX 历史 K 线（默认日线，最多约 1500 根，只含分析日之前）上回测一条做多规则，返回全样本与样本外（最后30%）统计。
    entry_expr：pandas 表达式，可用 o h l c v（Series），例如 "(c < c.rolling(20).min().shift(1)) & (v > v.rolling(20).mean()*1.5)"。
    第 i 根收盘信号成立 → 第 i+1 根开盘买入；sl_pct 止损%，tp_pct 止盈%（可空），max_bars 最长持有K线数；手续费单边0.1%。
    symbol 如 BTC-USD；bar 取 1D 或 4H 或 1H。回测只是参考证据，样本少（<30笔）不可信。"""
    inst = okx_data.to_inst(symbol) or symbol
    b = {"1D": "1Dutc"}.get(bar, bar)
    df = history(inst, b, 1500)
    try:
        res = ai_backtest().run(df, entry_expr=entry_expr, side="long", sl_pct=sl_pct, tp_pct=tp_pct,
                                max_bars=max_bars, fee=0.001, oos_frac=0.3)
    except Exception as e:
        return f"回测失败：{type(e).__name__}: {e}"
    res = {k: v for k, v in res.items() if not k.startswith("交易明细")}
    return json.dumps({k: v for k, v in res.items() if k != "trades"}, ensure_ascii=False, default=str)[:3000]


# ------------------------------------------------------------------ 工具输出上限
# 2026-11-14 用量事故定位结论之一：分析师工具（get_stock_data / get_indicators / get_news /
# get_prediction_markets / get_macro_indicators 等 vendor 工具）返回的是原始数据载荷，全系统
# 没有任何体积上限；这些结果进入图的共享消息状态后会被后续每一轮分析师调用反复重发，是全仓
# 唯一能产生十万级 token 的输入通道。当晚 gpt 席位图内输入由 20 万涨到 54 万即与此通道吻合。
# 这里在交易台的工具安装咽喉给每个分析师工具套上输出上限：只截字符串结果的尾部并注明，
# 工具名/参数 schema/调用方式完全不变，分析师的决策语义不变（数据本来就是抽样看的）。
TOOL_OUTPUT_CAP = 24_000   # 单个工具结果进入 LLM 上下文的字符上限（实测正常载荷 ≤ 约 1.1 万字符）


def cap_tool_output(t, cap: int = TOOL_OUTPUT_CAP):
    """给一个 langchain 工具套输出上限（幂等：已套过的原样返回）。非字符串结果不处理。"""
    if getattr(t, "_ta_desk_capped", False):
        return t
    from langchain_core.tools import StructuredTool
    orig = t

    def _run(**kwargs):
        out = orig.func(**kwargs) if getattr(orig, "func", None) else orig.invoke(kwargs)
        if isinstance(out, str) and len(out) > cap:
            return (out[:cap] + f"\n……（工具输出过长，代码已截断：原文共 {len(out)} 字符，"
                    f"仅保留前 {cap} 字符；需要更细的数据请缩小查询范围）")
        return out

    w = StructuredTool.from_function(func=_run, name=orig.name,
                                     description=orig.description, args_schema=orig.args_schema)
    w._ta_desk_capped = True
    return w


def install_tool_caps():
    """把全部分析师工具换成带输出上限的版本（模块 TOOLS 与图执行规格同步替换，幂等）。"""
    import dataclasses

    import tradingagents.graph.analyst_execution as ae
    from tradingagents.agents.analysts import fundamentals_analyst as fa
    from tradingagents.agents.analysts import market_analyst as ma
    from tradingagents.agents.analysts import news_analyst as na
    for key, mod in (("market", ma), ("news", na), ("fundamentals", fa)):
        capped = tuple(cap_tool_output(t) for t in mod.TOOLS)
        mod.TOOLS = capped
        spec = ae.ANALYST_NODE_SPECS.get(key)
        if spec is not None:
            ae.ANALYST_NODE_SPECS[key] = dataclasses.replace(spec, tools=capped)


def install_market_tools():
    """把 run_backtest 加入 TradingAgents 市场分析师的工具集，并给全部分析师工具套输出上限（幂等）。"""
    import dataclasses
    import tradingagents.agents.analysts.market_analyst as ma
    import tradingagents.graph.analyst_execution as ae
    if not any(getattr(t, "name", "") == "run_backtest" for t in ma.TOOLS):
        ma.TOOLS = tuple(ma.TOOLS) + (run_backtest,)
    install_tool_caps()
    ae.ANALYST_NODE_SPECS["market"] = dataclasses.replace(ae.ANALYST_NODE_SPECS["market"], tools=ma.TOOLS)
