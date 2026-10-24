"""FRED 宏观数据：给新闻分析师（news）的日常输入 + 日报「宏观数据（FRED）」小节。
密钥只从环境变量 FRED_API_KEY 读取，绝不打印/记录；日志中的 URL 一律去掉 api_key。
任何失败都优雅降级（用缓存或标注不可用），不抛异常。"""
from __future__ import annotations

import datetime as dt
import logging
import os
import re

from desk import settings

log = logging.getLogger("ta-desk.macro")
API = "https://api.stlouisfed.org/fred/series/observations"
CACHE = settings.STATE / "macro_cache.json"
DEFAULT_SERIES = [
    {"id": "DGS10", "name": "10年美债收益率", "unit": "%"},
    {"id": "DGS2", "name": "2年美债收益率", "unit": "%"},
    {"id": "DFF", "name": "联邦基金有效利率", "unit": "%"},
    {"id": "DTWEXBGS", "name": "广义美元指数", "unit": ""},
    {"id": "T10YIE", "name": "10年盈亏平衡通胀", "unit": "%"},
    {"id": "CPIAUCSL", "name": "CPI", "unit": "", "yoy": True},
    {"id": "VIXCLS", "name": "VIX", "unit": ""},
]


def redact(s) -> str:
    return re.sub(r"(api_key=)[^&\s'\"]+", r"\1***", str(s))


def _series(cfg) -> list[dict]:
    m = (cfg or {}).get("macro") or {}
    out = []
    for s in m.get("fred_series") or DEFAULT_SERIES:
        out.append({"id": s} if isinstance(s, str) else dict(s))
    return out


def enabled(cfg) -> bool:
    return bool(((cfg or {}).get("macro") or {}).get("enabled", True))


def fetch_observations(sid: str, key: str, start: str) -> list[tuple[str, float]]:
    import requests
    p = {"series_id": sid, "api_key": key, "file_type": "json", "observation_start": start, "sort_order": "asc"}
    try:
        r = requests.get(API, params=p, timeout=15)
        r.raise_for_status()
        obs = r.json().get("observations", [])
    except Exception as e:  # 异常文本可能带 URL → 脱敏
        raise RuntimeError(redact(f"{type(e).__name__}: {e}")) from None
    out = []
    for o in obs:
        try:
            out.append((o["date"], float(o["value"])))
        except (KeyError, TypeError, ValueError):   # FRED 缺值为 "."
            continue
    return out


def _at_or_before(obs, date: dt.date):
    v = None
    for d, x in obs:
        if dt.date.fromisoformat(d) <= date:
            v = (d, x)
        else:
            break
    return v


def compute(spec: dict, obs: list[tuple[str, float]]) -> dict:
    if not obs:
        return {**spec, "ok": False, "error": "无数据"}
    d, v = obs[-1]
    last = dt.date.fromisoformat(d)
    r = {**spec, "ok": True, "date": d, "value": v}
    if spec.get("yoy"):
        b = _at_or_before(obs, last.replace(year=last.year - 1))
        r["yoy_pct"] = round((v / b[1] - 1) * 100, 2) if b else None
    else:
        for k, days in (("chg_1w", 7), ("chg_1m", 30)):
            b = _at_or_before(obs, last - dt.timedelta(days=days))
            r[k] = round(v - b[1], 4) if b else None
    return r


def _load_cache() -> dict:
    try:
        return settings.read_json(CACHE, {}) or {}
    except Exception:
        return {}


def get(cfg, date: str | None = None, force: bool = False) -> dict:
    """返回 {"date", "fetched_at", "items": [...], "source": fresh|cache|stale_cache|disabled|unavailable}。从不抛异常。"""
    date = date or settings.today()
    if not enabled(cfg):
        return {"date": date, "items": [], "source": "disabled"}
    cache = _load_cache()
    if not force and cache.get("date") == date and cache.get("items") and all(i.get("ok") for i in cache["items"]):
        return {**cache, "source": "cache"}
    key = os.environ.get("FRED_API_KEY", "").strip()
    old = {i["id"]: i for i in cache.get("items", []) if i.get("ok")}
    start = (dt.date.fromisoformat(date) - dt.timedelta(days=430)).isoformat()
    items = []
    for spec in _series(cfg):
        try:
            if not key:
                raise RuntimeError("未设置 FRED_API_KEY")
            items.append(compute(spec, fetch_observations(spec["id"], key, start)))
        except Exception as e:
            msg = redact(e)
            log.warning("FRED %s 拉取失败：%s", spec["id"], msg)
            items.append({**old[spec["id"]], "stale": True} if spec["id"] in old else {**spec, "ok": False, "error": msg[:120]})
    if not any(i.get("ok") for i in items):
        return {"date": date, "items": items, "source": "unavailable"}
    res = {"date": date, "fetched_at": settings.now_str(), "items": items}
    try:
        settings.write_json(CACHE, res)
    except Exception as e:
        log.warning("写宏观缓存失败：%s", redact(e))
    return {**res, "source": "fresh" if not any(i.get("stale") for i in items) else "stale_cache"}


def safe_get(cfg, date=None) -> dict:
    try:
        return get(cfg, date)
    except Exception as e:
        log.warning("宏观模块异常：%s", redact(e))
        return {"date": date, "items": [], "source": "unavailable"}


def _n(x, nd=2, sign=False):
    return "—" if x is None else (f"{x:+.{nd}f}" if sign else f"{x:.{nd}f}")


def brief(m: dict) -> str:
    """给 news 角色的中文简短摘要。"""
    if not m or m.get("source") == "disabled":
        return ""
    ok = [i for i in m.get("items", []) if i.get("ok")]
    if not ok:
        return "## 宏观数据（FRED）\n本日宏观数据不可用（FRED 拉取失败且无缓存），请勿臆测宏观数值。"
    L = ["## 宏观数据（FRED，美国宏观/流动性背景，供判断风险偏好）"]
    for i in ok:
        nm = i.get("name") or i["id"]
        if i.get("yoy"):
            s = f"- {nm}（{i['id']}）{i['date']}：{_n(i['value'])}，同比 {_n(i.get('yoy_pct'), sign=True)}%"
        else:
            u = i.get("unit", "")
            s = f"- {nm}（{i['id']}）{i['date']}：{_n(i['value'])}{u}，较1周 {_n(i.get('chg_1w'), sign=True)}，较1月 {_n(i.get('chg_1m'), sign=True)}"
        L.append(s + ("（缓存旧值）" if i.get("stale") else ""))
    bad = [i["id"] for i in m["items"] if not i.get("ok")]
    if bad:
        L.append("不可用：" + "、".join(bad))
    L.append("注：FRED 数据有发布滞后（日频约1-2个工作日，CPI 为月度），仅作背景，不是加密货币的直接信号。")
    return "\n".join(L)


def report_lines(m: dict) -> list[str]:
    if not m or m.get("source") == "disabled":
        return []
    L = ["## 宏观数据（FRED）", ""]
    if not m.get("items"):
        return L + ["宏观数据不可用。", ""]
    src = {"fresh": "今日拉取", "cache": "今日缓存", "stale_cache": "部分用旧缓存", "unavailable": "不可用"}.get(m.get("source"), m.get("source"))
    L += [f"来源：FRED（{src}{'，' + m['fetched_at'] if m.get('fetched_at') else ''}）", "",
          "| 指标 | 代码 | 最新值 | 日期 | 较1周 | 较1月 / 同比 |", "|---|---|---|---|---|---|"]
    for i in m["items"]:
        nm = i.get("name") or i["id"]
        if not i.get("ok"):
            L.append(f"| {nm} | {i['id']} | 不可用 | — | — | — |"); continue
        tail = (f"同比 {_n(i.get('yoy_pct'), sign=True)}%" if i.get("yoy") else _n(i.get("chg_1m"), sign=True))
        L.append(f"| {nm}{'（旧缓存）' if i.get('stale') else ''} | {i['id']} | {_n(i['value'])}{i.get('unit', '')} | {i['date']} | "
                 f"{'—' if i.get('yoy') else _n(i.get('chg_1w'), sign=True)} | {tail} |")
    return L + [""]
