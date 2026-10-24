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


def install_market_tools():
    """把 run_backtest 加入 TradingAgents 市场分析师的工具集（幂等）。"""
    import dataclasses
    import tradingagents.agents.analysts.market_analyst as ma
    import tradingagents.graph.analyst_execution as ae
    if any(getattr(t, "name", "") == "run_backtest" for t in ma.TOOLS):
        return
    ma.TOOLS = tuple(ma.TOOLS) + (run_backtest,)
    ae.ANALYST_NODE_SPECS["market"] = dataclasses.replace(ae.ANALYST_NODE_SPECS["market"], tools=ma.TOOLS)
