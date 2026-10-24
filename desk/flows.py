"""资金流/衍生品分析师（新增角色 flows）的数据：全部来自免费公开接口。
- OKX 公共接口：永续资金费率（当前+历史）、持仓量、多空账户比、主动买卖量、持仓量/成交额
- DefiLlama：稳定币总流通市值（场外资金"弹药"）
ETF 资金流（farside 等）有 Cloudflare 拦截，盒子无法免费稳定获取，暂不接入。"""
from __future__ import annotations

import datetime as _dt
import time

import requests

OKX = "https://www.okx.com/api/v5/"
_cache: dict = {}


def _get(url, ttl=600):
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    for i in range(3):
        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            d = r.json()
            _cache[url] = (time.time(), d)
            return d
        except Exception:
            if i == 2:
                raise
            time.sleep(1 + i)


def _d(ms):
    return _dt.datetime.fromtimestamp(int(ms) / 1000, _dt.timezone.utc).strftime("%m-%d")


FETCH_ITEMS = 7   # fetch 采集项数（funding_now/funding_hist/oi_usd/ls_ratio/oi_vol/taker/stables），L4 用于算失败比例


def fetch(ccy: str) -> dict:
    out, errs = {}, []
    swap = f"{ccy}-USDT-SWAP"
    def safe(k, fn):
        try:
            out[k] = fn()
        except Exception as e:
            errs.append(f"{k}: {type(e).__name__}")
    safe("funding_now", lambda: float(_get(f"{OKX}public/funding-rate?instId={swap}")["data"][0]["fundingRate"]))
    safe("funding_hist", lambda: [(int(x["fundingTime"]), float(x["realizedRate"] or x["fundingRate"]))
                                  for x in _get(f"{OKX}public/funding-rate-history?instId={swap}&limit=100")["data"]][::-1])
    safe("oi_usd", lambda: float(_get(f"{OKX}public/open-interest?instType=SWAP&instId={swap}")["data"][0]["oiUsd"]))
    safe("ls_ratio", lambda: [(int(a), float(b)) for a, b in _get(f"{OKX}rubik/stat/contracts/long-short-account-ratio?ccy={ccy}&period=1D")["data"]][::-1])
    safe("oi_vol", lambda: [(int(a), float(b), float(c)) for a, b, c in _get(f"{OKX}rubik/stat/contracts/open-interest-volume?ccy={ccy}&period=1D")["data"]][::-1])
    safe("taker", lambda: [(int(a), float(b), float(c)) for a, b, c in _get(f"{OKX}rubik/stat/taker-volume?ccy={ccy}&instType=CONTRACTS&period=1D")["data"]][::-1])
    def stable():
        d = _get("https://stablecoins.llama.fi/stablecoincharts/all", ttl=3600)
        return [(int(x["date"]) * 1000, float(x["totalCirculatingUSD"]["peggedUSD"])) for x in d[-40:]]
    safe("stables", stable)
    out["errors"] = errs
    return out


def brief(ccy: str, data: dict | None = None) -> str:
    d = data or fetch(ccy)
    L = [f"## {ccy} 衍生品与资金流数据（OKX 公共接口 + DefiLlama，截至最近已收盘）"]
    if "funding_now" in d:
        L.append(f"- 当前永续资金费率（8h）：{d['funding_now'] * 100:.4f}%（年化约 {d['funding_now'] * 3 * 365 * 100:.1f}%）")
    if d.get("funding_hist"):
        fh = [r for _, r in d["funding_hist"]]
        L.append(f"- 近 {len(fh)} 期资金费率：均值 {sum(fh) / len(fh) * 100:.4f}%，最近 9 期均值 {sum(fh[-9:]) / len(fh[-9:]) * 100:.4f}%，"
                 f"最高 {max(fh) * 100:.4f}%，最低 {min(fh) * 100:.4f}%，负费率期数 {sum(1 for x in fh if x < 0)}")
    if "oi_usd" in d:
        L.append(f"- OKX 永续持仓量：{d['oi_usd'] / 1e9:.3f} 十亿美元")
    if d.get("oi_vol"):
        rows = d["oi_vol"][-10:]
        L.append("- 全合约持仓量/成交额（十亿美元，近10日）：" + "，".join(f"{_d(t)} {oi / 1e9:.2f}/{v / 1e9:.2f}" for t, oi, v in rows))
    if d.get("ls_ratio"):
        L.append("- 多空账户比（近10日）：" + "，".join(f"{_d(t)} {r:.2f}" for t, r in d["ls_ratio"][-10:]))
    if d.get("taker"):
        L.append("- 主动卖/买成交（十亿美元，近7日；>1 表示卖方主动）：" + "，".join(
            f"{_d(t)} {s / b:.2f}" for t, s, b in d["taker"][-7:] if b))
    if d.get("stables"):
        s = d["stables"]
        L.append(f"- 稳定币总流通：{s[-1][1] / 1e9:.1f} 十亿美元，7日变化 {(s[-1][1] / s[-8][1] - 1) * 100:+.2f}%，30日 {(s[-1][1] / s[-31][1] - 1) * 100:+.2f}%")
    L.append("- 未接入：ETF 资金流、交易所链上净流入（无免费稳定来源）")
    if d.get("errors"):
        L.append("- 获取失败：" + "；".join(d["errors"]))
    return "\n".join(L)


PROMPT = """你是加密货币交易台的【资金流/衍生品分析师】。只依据下面的数据（资金费率、持仓量、多空比、主动买卖、稳定币流通）判断 {ccy} 未来几天的拥挤度与资金面方向。
不要分析 K 线形态和新闻（其他分析师负责）。说明：哪些指标显示杠杆拥挤/去杠杆、空头或多头挤压风险、资金流入流出。数据缺失要说明。
用中文写 300~600 字报告，最后给出手册要求的【立场】行。

{data}
"""


def analyze(cfg, ccy: str, tracker=None) -> dict:
    from desk import llm as dllm, roles
    raw = fetch(ccy)
    n_err = len(raw.get("errors") or [])
    missing = n_err / FETCH_ITEMS >= float((cfg.get("data_quality") or {}).get("flows_missing_ratio", 0.5))   # L4
    data_txt = brief(ccy, raw)
    msgs = roles.PlaybookLLM(dllm.make_llm(cfg, "quick", tracker, role="flows"), "flows")
    tok = dllm.STAGE.set(f"{ccy}-USDT/资金流分析师")
    try:
        res = msgs.invoke([("system", "你是严谨的加密衍生品数据分析师。"), ("human", PROMPT.format(ccy=ccy, data=data_txt))])
        report = res.content if isinstance(res.content, str) else str(res.content)
    except Exception as e:
        report = f"资金流分析失败（{type(e).__name__}）。\n\n{data_txt}"
    finally:
        dllm.STAGE.reset(tok)
    return {"data": data_txt, "report": report, "data_missing": missing, "fetch_errors": n_err}
