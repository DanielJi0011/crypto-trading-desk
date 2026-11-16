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
# P1（2026-11-15 LLM 数据流审查）：按工具分化上限。K 线 CSV（get_stock_data）是循环里最大的
# 单条结果，日线块本身就是载荷（4H/1H 参考块已从数据源头去掉，见 okx_data.okx_get_stock_data），
# 上限收紧到 12K；其余工具维持 24K。
TOOL_OUTPUT_CAPS = {"get_stock_data": 12_000}
# 截断留痕（P1 硬约束：工具数据被上限裁掉绝不静默）：每次截断记一条（工具名/原长/上限/当前阶段），
# analysis 把本币条目挂到 fs["_tool_truncations"]，stances.extract 再落到对应角色立场的 data_note，
# 随决策记录进日报数据缺口口径；同时结果文本内联英文注记，分析师本人也看得见。
TOOL_TRUNCATIONS: list[dict] = []


def cap_for(tool_name: str) -> int:
    return TOOL_OUTPUT_CAPS.get(tool_name, TOOL_OUTPUT_CAP)


def cap_tool_output(t, cap: int | None = None):
    """给一个 langchain 工具套输出上限（幂等：已套过的原样返回）。非字符串结果不处理。
    cap 缺省按工具名查 TOOL_OUTPUT_CAPS（get_stock_data=12K，其余 24K）。"""
    if getattr(t, "_ta_desk_capped", False):
        return t
    from langchain_core.tools import StructuredTool
    orig = t
    if cap is None:
        cap = cap_for(orig.name)

    def _run(**kwargs):
        out = orig.func(**kwargs) if getattr(orig, "func", None) else orig.invoke(kwargs)
        if isinstance(out, str) and len(out) > cap:
            try:
                from desk import llm as _dllm
                stage = _dllm.STAGE.get()
            except Exception:
                stage = ""
            TOOL_TRUNCATIONS.append({"tool": orig.name, "chars": len(out), "cap": cap, "stage": stage})
            return (out[:cap] + f"\n... [tool output truncated by code: {len(out)} chars total, "
                    f"first {cap} kept; narrow the query if you need finer data]")
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


_ANALYST_GRAPH_ORIG = None
MARKET_TOOL_ROUNDS = 8   # 市场分析师的工具轮数例外（P1 审查口径：回测路径保留 5+ 次取数的空间）


def install_tool_loop_discipline(cfg):
    """P1：分析师工具循环纪律。vendor 的 max_tool_rounds 是全图单一值（台子配置已降到 4，
    见 llm.ta_config 的代码默认值与 config llm.max_tool_rounds），但 run_backtest 只在市场
    分析师手里、回测取数偶尔需要 5 轮以上——按审查的例外口径，只给市场分析师保留
    llm.market_max_tool_rounds（默认 8）。实现是运行时包裹 vendor 的 _analyst_graph
    （与 roles.install 的工厂包裹同一手法），不改 vendor 源码；幂等。"""
    global _ANALYST_GRAPH_ORIG
    if _ANALYST_GRAPH_ORIG is not None:
        return
    import tradingagents.graph.setup as gsetup
    _ANALYST_GRAPH_ORIG = gsetup._analyst_graph
    market_rounds = int(((cfg or {}).get("llm") or {}).get("market_max_tool_rounds", MARKET_TOOL_ROUNDS))

    def _patched(spec, agent, max_tool_rounds):
        if getattr(spec, "key", None) == "market":
            max_tool_rounds = market_rounds
        return _ANALYST_GRAPH_ORIG(spec, agent, max_tool_rounds)

    gsetup._analyst_graph = _patched
