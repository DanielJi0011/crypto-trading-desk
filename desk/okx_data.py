"""OKX 行情数据接入 TradingAgents。

- 日线：OKX 1Dutc（UTC 日，北京时间 08:00 收盘），只用已收盘K线
- 替换 TradingAgents 的 Yahoo 价格加载函数（load_ohlcv / get_closes）：加密货币走 OKX，失败回退 Yahoo
- 注册 "okx" 数据源到 TradingAgents 路由（get_stock_data）
"""
from __future__ import annotations

import logging
import random
import threading
import time

import pandas as pd

from desk import settings

log = logging.getLogger("desk.okx")
_client = None
_cache: dict = {}
CACHE_TTL = 600

# OKX 公共行情按 IP 限流（candles 40 次/2 秒、history-candles 20 次/2 秒，超限返回 50011）。
# 2026-11-09 晚场事故：分析阶段多指标并发拉 K 线 + 翻页间隔仅 0.1 秒，瞬间打满 history-candles
# 窗口，BTC/ETH 决策失败。对策：本进程内所有 K 线请求串行化并统一节流（相邻请求至少间隔
# _PACE_INTERVAL 秒，叠加请求本身的网络耗时后稳低于 20 次/2 秒）；50011 在 _retry 里走
# 指数退避，不再用短间隔整段硬重试。
_candle_lock = threading.Lock()
_last_candle_at = 0.0
_PACE_INTERVAL = 0.15


def _paced_candles(fn):
    """在全局锁内执行一次 K 线请求，并与上一次 K 线请求保持最小间隔。"""
    global _last_candle_at
    with _candle_lock:
        wait = _PACE_INTERVAL - (time.time() - _last_candle_at)
        if wait > 0:
            time.sleep(wait)
        try:
            return fn()
        finally:
            _last_candle_at = time.time()


class _NoCreds:
    """非 None 的空凭据占位，布尔值为假。"""
    def __bool__(self):
        return False

    def __repr__(self):
        return "NoCreds"


_NO_CREDS = _NoCreds()


def client():
    global _client
    if _client is None:
        okx_client, _, _ = settings.ai_trader_modules()
        # 2b：传入显式的“无凭据”对象，避免 OKXClient 在 credentials=None 时调用 Credentials.from_env 读取实盘变量
        _client = okx_client.OKXClient(mode="signal_only", simulated=False, credentials=_NO_CREDS, timeout=15)
        _client.creds = None   # 只用公共接口，绝不带私钥（私有请求会因无凭据直接报错）
    return _client


def to_inst(symbol: str) -> str | None:
    """BTC-USD / BTCUSDT / BTC-USDT → BTC-USDT；非加密返回 None。"""
    from tradingagents.dataflows.symbols import crypto_base
    base = crypto_base(symbol)
    return f"{base}-USDT-SWAP" if base else None   # 纸面交易标的：OKX USDT 永续


def candles(inst_id: str, bar: str = "1Dutc", days: int = 420) -> pd.DataFrame:
    return _retry(lambda: _candles(inst_id, bar, days))


def _candles(inst_id: str, bar: str = "1Dutc", days: int = 420) -> pd.DataFrame:
    """已收盘K线，升序。列：ts, Date(naive UTC 日期或时间), Open, High, Low, Close, Volume, VolumeUSDT"""
    # 缓存按 (inst, bar) 存已拉到的最大窗口：不同指标/调用方要的天数不同（420/100/30…），
    # 只要缓存窗口够大就切尾返回，不再按天数各拉一遍（同一场内同一币的重复拉取是 50011 的主因之一）。
    key = (inst_id, bar)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL and len(hit[1]) >= days:
        return hit[1].tail(days).reset_index(drop=True).copy()
    c = client()
    rows = _paced_candles(lambda: c.get_candles(inst_id, bar=bar, limit=300))
    after = rows[-1][0] if rows else None
    while len(rows) < days and after:
        page = _paced_candles(lambda after=after: c.get_history_candles(inst_id, bar=bar, limit=100, after=after))
        if not page:
            break
        rows.extend(page)
        after = page[-1][0]
    df = pd.DataFrame([r[:9] for r in rows], columns=["ts", "Open", "High", "Low", "Close", "Volume", "volCcy", "VolumeUSDT", "confirm"])
    df = df[df.confirm == "1"].copy()
    for k in ["Open", "High", "Low", "Close", "Volume", "VolumeUSDT"]:
        df[k] = df[k].astype(float)
    df["ts"] = df.ts.astype("int64")
    df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    t = pd.to_datetime(df.ts, unit="ms", utc=True).dt.tz_localize(None)
    df["Date"] = t.dt.normalize() if bar.startswith("1D") else t
    df = df[["ts", "Date", "Open", "High", "Low", "Close", "Volume", "VolumeUSDT"]].reset_index(drop=True)
    _cache[key] = (time.time(), df)
    return df.tail(days).reset_index(drop=True).copy()


def _retry(fn, tries=4):
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            if i == tries - 1:
                raise
            if "50011" in str(e):
                # OKX 限流：指数退避 + 抖动，给限流窗口留恢复时间；配合 _paced_candles 的节流，
                # 重拉不会再把窗口打满（旧行为是 2/4/6 秒整段重拉，越限越压）。
                time.sleep(min(30.0, 3 * (2 ** i)) + random.uniform(0, 1))
            else:
                time.sleep(2 * (i + 1))


def funding_rate(inst_id: str) -> float:
    """OKX 当前资金费率（8h，公共接口）。现货代码自动换成对应永续。"""
    import requests
    swap = inst_id if inst_id.endswith("-SWAP") else inst_id + "-SWAP"
    r = requests.get(f"{settings.load_config()['data']['okx_base_url']}/api/v5/public/funding-rate?instId={swap}", timeout=15)
    return float(r.json()["data"][0]["fundingRate"])


def last_price(inst_id: str) -> float:
    return float(_retry(lambda: client().get_ticker(inst_id)["last"]))


# ------------------------------------------------------------------ TradingAgents 接入
_orig = {}
_FALLBACK: set = set()   # D1 第 1 点：本进程内日线/收盘价回退过 Yahoo 的品种（只用于报告，当日禁止开新仓）


def fallback_used(inst: str) -> bool:
    return inst in _FALLBACK


def okx_load_ohlcv(symbol: str, as_of_date: str, fill_gaps: bool = True) -> pd.DataFrame:
    inst = to_inst(symbol)
    if inst:
        try:
            df = candles(inst, settings.load_config()["data"]["daily_bar"], settings.load_config()["data"]["history_days"])
            df = df[df.Date <= pd.to_datetime(as_of_date).normalize()]
            if not df.empty:
                return df[["Date", "Open", "High", "Low", "Close", "Volume"]].reset_index(drop=True)
        except Exception as e:  # 回退 Yahoo
            log.warning("OKX 日线获取失败 %s：%s，回退 Yahoo（该币当日禁止开新仓）", inst, e)
            _FALLBACK.add(inst)
    return _orig["load_ohlcv"](symbol, as_of_date, fill_gaps=fill_gaps)


def okx_get_closes(symbol: str, start_date: str, end_date: str) -> pd.Series:
    """settlement 用：[start_date, end_date) 的日收盘价。"""
    inst = to_inst(symbol)
    if inst:
        try:
            df = candles(inst, "1Dutc", 420)
            s = pd.Series(df.Close.values, index=pd.DatetimeIndex(df.Date))
            return s[(s.index >= pd.Timestamp(start_date)) & (s.index < pd.Timestamp(end_date))]
        except Exception as e:
            log.warning("OKX 收盘价获取失败 %s：%s，回退 Yahoo（该币当日禁止开新仓）", inst, e)
            _FALLBACK.add(inst)
    return _orig["get_closes"](symbol, start_date, end_date)


def okx_get_stock_data(symbol: str, start_date: str, end_date: str) -> str:
    from tradingagents.dataflows.errors import NoMarketDataError
    inst = to_inst(symbol)
    if not inst:
        raise NoMarketDataError(symbol, symbol, "OKX 只服务加密货币")
    df = candles(inst, "1Dutc", 420)
    df = df[(df.Date >= pd.Timestamp(start_date)) & (df.Date <= pd.Timestamp(end_date))]
    if df.empty:
        raise NoMarketDataError(symbol, inst, f"OKX 在 {start_date}~{end_date} 没有已收盘日线")
    out = df[["Date", "Open", "High", "Low", "Close", "Volume", "VolumeUSDT"]].copy()
    out["Date"] = out.Date.dt.strftime("%Y-%m-%d")
    header = (f"# Price data for {symbol} from OKX spot {inst}, daily bars closing 00:00 UTC (08:00 Asia/Shanghai)\n"
              f"# {start_date} to {end_date}; Total records: {len(out)}; Volume in base units, VolumeUSDT in quote\n\n")
    return header + out.to_csv(index=False) + intraday_text(inst, end_date)


INTRADAY = (("4H", 42), ("1H", 48))   # 2026-10-23 用户要求：两场都提供 4H（约 7 天）与 1H（约 2 天）已收盘K线


def intraday_text(inst: str, end_date: str) -> str:
    """4H/1H 已收盘K线（UTC 开盘时刻），只取 end_date 当天及以前开盘且已收盘的；获取失败只写一行说明，不影响日线。"""
    out = []
    for bar, n in INTRADAY:
        try:
            df = candles(inst, bar, max(n, 100))
            df = df[df.Date < pd.Timestamp(end_date) + pd.Timedelta(days=1)].tail(n)
            if df.empty:
                continue
            t = df[["Date", "Open", "High", "Low", "Close", "Volume"]].copy()
            t["Date"] = t.Date.dt.strftime("%Y-%m-%d %H:%M")
            out.append(f"\n# {bar} closed bars for {inst} (open time UTC), last {len(t)}\n" + t.to_csv(index=False))
        except Exception as e:
            log.warning("OKX %s K线获取失败 %s：%s", bar, inst, e)
            out.append(f"\n# {bar} bars unavailable ({type(e).__name__})\n")
    return "".join(out)


def install():
    """把 OKX 数据源装进 TradingAgents（幂等）。"""
    import tradingagents.dataflows.router as router
    import tradingagents.dataflows.vendors.yahoo.market as ymarket
    import tradingagents.dataflows.vendors.yahoo.ohlcv as yohlcv
    import tradingagents.dataflows.vendors.yahoo.snapshot as ysnap
    import tradingagents.memory.settlement as settle
    if "load_ohlcv" not in _orig:
        _orig["load_ohlcv"] = yohlcv.load_ohlcv
        _orig["get_closes"] = ymarket.get_closes
    ymarket.load_ohlcv = okx_load_ohlcv
    ysnap.load_ohlcv = okx_load_ohlcv
    settle.get_closes = okx_get_closes
    router.VENDOR_METHODS["get_stock_data"]["okx"] = okx_get_stock_data
    from desk import untrusted          # S1：新闻/Reddit/StockTwits 文本加不可信定界符
    untrusted.install()
