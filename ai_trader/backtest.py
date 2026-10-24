"""通用"假设回测"工具：用 OKX 历史 K 线验证智能体自己提出的规则。

本模块不内置任何策略。规则由智能体提供，两种方式：
  1) 表达式：--entry "<表达式>" [--exit "<表达式>"]
     可用变量：o h l c v（pandas Series，按时间升序）、df、pd、abs/min/max
     表达式结果需为布尔 Series；第 i 根 K 线收盘为 True → 第 i+1 根开盘价入场
  2) 插件文件：hypotheses/<名称>.py，定义
        def entry(df) -> pd.Series[bool]       # 必需
        def exit(df)  -> pd.Series[bool]       # 可选
        PARAMS = {"side": "long", "sl_pct": 2, "tp_pct": 4, "max_bars": 20}  # 可选
止损/止盈在 K 线内按最高/最低价判定；同一根同时触及时保守地按止损计。
"""
from __future__ import annotations

import importlib.util
import math
from pathlib import Path
from typing import Optional

import pandas as pd

BAR_MS = {"1m": 60e3, "3m": 180e3, "5m": 300e3, "15m": 900e3, "30m": 1800e3,
          "1H": 3600e3, "2H": 7200e3, "4H": 14400e3, "1D": 86400e3}


def candles_to_df(rows: list) -> pd.DataFrame:
    cols = ["ts", "o", "h", "l", "c", "v", "vol_ccy", "vol_quote", "confirm"]
    df = pd.DataFrame([r[:9] for r in rows], columns=cols[: len(rows[0][:9])] if rows else cols)
    if df.empty:
        return df
    for k in ["o", "h", "l", "c", "v", "vol_ccy", "vol_quote"]:
        if k in df:
            df[k] = df[k].astype(float)
    df["ts"] = df["ts"].astype("int64")
    df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    df["time"] = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.tz_convert("Asia/Shanghai")
    return df


def fetch_history(client, inst_id: str, bar: str = "15m", bars: int = 1000,
                  since_ts: Optional[int] = None, max_pages: int = 200) -> pd.DataFrame:
    """向前翻页拉取历史 K 线（history-candles 每页最多 100 根）。"""
    out, after = [], None
    for _ in range(max_pages):
        page = client.get_history_candles(inst_id, bar=bar, limit=100, after=after)
        if not page:
            break
        out.extend(page)
        oldest = int(page[-1][0])
        after = str(oldest)
        if len(out) >= bars or (since_ts and oldest <= since_ts):
            break
    df = candles_to_df(out)
    if since_ts is not None and not df.empty:
        df = df[df["ts"] >= since_ts - BAR_MS.get(bar, 0)].reset_index(drop=True)
    elif len(df) > bars:
        df = df.iloc[-bars:].reset_index(drop=True)
    return df


def add_atr(df: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    pc = df["c"].shift(1)
    tr = pd.concat([df["h"] - df["l"], (df["h"] - pc).abs(), (df["l"] - pc).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(n).mean()
    return df


def align_btc(df: pd.DataFrame, btc: pd.DataFrame) -> pd.DataFrame:
    """把 BTC K 线按 ts 对齐到 df 的索引上。"""
    b = btc.set_index("ts").reindex(df["ts"].values)
    b.index = df.index
    b["ts"] = df["ts"].values
    return b


SAFE_BUILTINS = {"abs": abs, "min": min, "max": max, "len": len, "round": round}


def eval_expr(expr: str, df: pd.DataFrame) -> pd.Series:
    ns = {"o": df["o"], "h": df["h"], "l": df["l"], "c": df["c"], "v": df["v"], "df": df, "pd": pd}
    res = eval(expr, {"__builtins__": SAFE_BUILTINS}, ns)  # noqa: S307 —— 表达式来自智能体自身
    if not isinstance(res, pd.Series):
        res = pd.Series([bool(res)] * len(df))
    return res.fillna(False).astype(bool)


def load_plugin(path: str):
    import sys
    p = Path(path)
    if str(p.parent.resolve()) not in sys.path:
        sys.path.insert(0, str(p.parent.resolve()))
    spec = importlib.util.spec_from_file_location(p.stem, p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, "entry"):
        raise ValueError("插件必须定义 entry(df)")
    return mod


def simulate(df: pd.DataFrame, entry: pd.Series, exit_: Optional[pd.Series] = None,
             side: str = "long", sl_pct: float = 2.0, tp_pct: Optional[float] = None,
             max_bars: Optional[int] = None, fee: float = 0.001, sl_atr: Optional[float] = None,
             tp_r: Optional[float] = None) -> dict:
    """sl_atr 给定时止损距离 = sl_atr × ATR(信号K线)；tp_r 给定时止盈 = tp_r × 止损距离。
    跳空越过止损/止盈时按开盘价成交。fee 为单边成本（手续费+滑点）。"""
    if sl_atr is not None and "atr" not in df:
        add_atr(df)
    trades = []
    i, n = 1, len(df)
    sign = 1 if side == "long" else -1
    while i < n:
        if not entry.iloc[i - 1]:
            i += 1
            continue
        ep = df["o"].iloc[i]
        if sl_atr is not None:
            a = df["atr"].iloc[i - 1]
            if not a > 0:
                i += 1
                continue
            dist = sl_atr * a
        else:
            dist = ep * sl_pct / 100
        sl = ep - sign * dist
        tp = ep + sign * tp_r * dist if tp_r else (ep * (1 + sign * tp_pct / 100) if tp_pct else None)
        j, xp, why = i, None, None
        while j < n:
            hi, lo = df["h"].iloc[j], df["l"].iloc[j]
            op = df["o"].iloc[j]
            if (side == "long" and lo <= sl) or (side == "short" and hi >= sl):
                xp, why = (min(sl, op) if side == "long" else max(sl, op)), "止损"
            elif tp is not None and ((side == "long" and hi >= tp) or (side == "short" and lo <= tp)):
                xp, why = (tp if j == i else (max(tp, op) if side == "long" else min(tp, op))), "止盈"
            elif exit_ is not None and exit_.iloc[j]:
                xp, why = df["c"].iloc[j], "离场条件"
            elif max_bars and j - i + 1 >= max_bars:
                xp, why = df["c"].iloc[j], "持仓超时"
            if xp is not None:
                break
            j += 1
        if xp is None:
            j, xp, why = n - 1, df["c"].iloc[-1], "数据结束"
        ret = sign * (xp - ep) / ep - 2 * fee
        trades.append({"entry_time": str(df["time"].iloc[i]), "exit_time": str(df["time"].iloc[j]),
                       "entry_px": ep, "exit_px": xp, "ret_pct": round(ret * 100, 4), "exit_reason": why,
                       "bars": j - i + 1})
        i = j + 1
    return {"trades": trades, "stats": stats([t["ret_pct"] / 100 for t in trades])}


def stats(rets: list[float]) -> dict:
    if not rets:
        return {"交易次数": 0}
    eq, peak, mdd = 1.0, 1.0, 0.0
    for r in rets:
        eq *= 1 + r
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    pf = (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else math.inf
    return {
        "交易次数": len(rets),
        "胜率%": round(len(wins) / len(rets) * 100, 2),
        "平均每笔%": round(sum(rets) / len(rets) * 100, 4),
        "累计收益%(复利)": round((eq - 1) * 100, 4),
        "最大回撤%": round(mdd * 100, 4),
        "盈亏比(profit factor)": round(pf, 3) if pf != math.inf else "inf",
        "平均盈利%": round(sum(wins) / len(wins) * 100, 4) if wins else 0,
        "平均亏损%": round(sum(losses) / len(losses) * 100, 4) if losses else 0,
    }


def run(df: pd.DataFrame, entry_expr: Optional[str] = None, exit_expr: Optional[str] = None,
        plugin: Optional[str] = None, side: str = "long", sl_pct: float = 2.0,
        tp_pct: Optional[float] = None, max_bars: Optional[int] = None, fee: float = 0.001,
        oos_frac: float = 0.3, btc: Optional[pd.DataFrame] = None) -> dict:
    params = dict(side=side, sl_pct=sl_pct, tp_pct=tp_pct, max_bars=max_bars)
    if plugin:
        mod = load_plugin(plugin)
        params.update(getattr(mod, "PARAMS", {}))
        kw = {"btc": align_btc(df, btc)} if getattr(mod, "NEEDS_BTC", False) else {}
        ent = pd.Series(mod.entry(df, **kw), index=df.index).fillna(False).astype(bool)
        ex = pd.Series(mod.exit(df, **kw), index=df.index).fillna(False).astype(bool) if hasattr(mod, "exit") else None
    else:
        if not entry_expr:
            raise ValueError("需要 --entry 表达式或 --plugin 插件")
        ent = eval_expr(entry_expr, df)
        ex = eval_expr(exit_expr, df) if exit_expr else None
    full = simulate(df, ent, ex, fee=fee, **params)
    out = {"参数": {**params, "fee": fee, "K线数": len(df),
                   "区间": [str(df["time"].iloc[0]), str(df["time"].iloc[-1])] if len(df) else None},
           "全样本": full["stats"],
           "持有不动参考%": round((df["c"].iloc[-1] / df["o"].iloc[0] - 1) * 100, 4) if len(df) else None}
    if 0 < oos_frac < 1 and len(df) > 50:
        cut = int(len(df) * (1 - oos_frac))
        a = df.iloc[:cut].reset_index(drop=True)
        b = df.iloc[cut:].reset_index(drop=True)
        ea, eb = ent.iloc[:cut].reset_index(drop=True), ent.iloc[cut:].reset_index(drop=True)
        xa = ex.iloc[:cut].reset_index(drop=True) if ex is not None else None
        xb = ex.iloc[cut:].reset_index(drop=True) if ex is not None else None
        out["样本内"] = simulate(a, ea, xa, fee=fee, **params)["stats"]
        out["样本外"] = simulate(b, eb, xb, fee=fee, **params)["stats"]
    out["交易明细(最后20笔)"] = full["trades"][-20:]
    return out
