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
    """资金面数据文本（只进机器人提示词与摘要构建，不进日报）。P4（2026-11-15）：紧凑英文
    + 稳定键名（funding_now (8h): / oi_usd):），供 desk/digest 的完整性校验正则定位。"""
    d = data or fetch(ccy)
    L = [f"## {ccy} derivatives & flows data (OKX public API + DefiLlama, as of latest close)"]
    if "funding_now" in d:
        L.append(f"- funding_now (8h): {d['funding_now'] * 100:+.4f}% (annualized approx {d['funding_now'] * 3 * 365 * 100:.1f}%)")
    if d.get("funding_hist"):
        fh = [r for _, r in d["funding_hist"]]
        L.append(f"- funding history, last {len(fh)} periods: mean {sum(fh) / len(fh) * 100:.4f}%, last-9 mean {sum(fh[-9:]) / len(fh[-9:]) * 100:.4f}%, "
                 f"max {max(fh) * 100:.4f}%, min {min(fh) * 100:.4f}%, negative periods {sum(1 for x in fh if x < 0)}")
    if "oi_usd" in d:
        L.append(f"- OKX perpetual open interest (oi_usd): {d['oi_usd'] / 1e9:.3f} billion USD")
    if d.get("oi_vol"):
        rows = d["oi_vol"][-10:]
        L.append("- open interest / volume, all contracts (billion USD, last 10d): " + ", ".join(f"{_d(t)} {oi / 1e9:.2f}/{v / 1e9:.2f}" for t, oi, v in rows))
    if d.get("ls_ratio"):
        L.append("- long/short account ratio (last 10d): " + ", ".join(f"{_d(t)} {r:.2f}" for t, r in d["ls_ratio"][-10:]))
    if d.get("taker"):
        L.append("- taker sell/buy volume (billion USD, last 7d; >1 = sellers aggressive): " + ", ".join(
            f"{_d(t)} {s / b:.2f}" for t, s, b in d["taker"][-7:] if b))
    if d.get("stables"):
        s = d["stables"]
        L.append(f"- total stablecoin circulation: {s[-1][1] / 1e9:.1f} billion USD, 7d change {(s[-1][1] / s[-8][1] - 1) * 100:+.2f}%, 30d {(s[-1][1] / s[-31][1] - 1) * 100:+.2f}%")
    L.append("- not connected: ETF flows, exchange on-chain net flows (no free stable source)")
    if d.get("errors"):
        L.append("- fetch failures: " + "; ".join(d["errors"]))
    return "\n".join(L)


PROMPT = """You are the flows/derivatives analyst of a crypto trading desk. Using ONLY the data below (funding rate, open interest, long/short ratio, taker buy/sell, stablecoin circulation), judge {ccy}'s crowding and flows direction over the next few days.
Do not analyze candle patterns or news (other analysts cover those). Cover: which indicators show leverage crowding / deleveraging, short- or long-squeeze risk, capital inflows/outflows. State plainly what data is missing.
Write the report in Chinese, 300-600 characters, and finish with the stance line your playbook requires.

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
        res = msgs.invoke([("system", "You are a rigorous crypto derivatives data analyst."), ("human", PROMPT.format(ccy=ccy, data=data_txt))])
        report = res.content if isinstance(res.content, str) else str(res.content)
    except Exception as e:
        report = f"Flows analysis failed ({type(e).__name__}).\n\n{data_txt}"
    finally:
        dllm.STAGE.reset(tok)
    return {"data": data_txt, "report": report, "data_missing": missing, "fetch_errors": n_err}
