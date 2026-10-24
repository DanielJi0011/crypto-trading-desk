"""B1 对照组（纸面记账，不调用 LLM，不下任何单）。

由 run_daily 末尾调用 update()。每个对照组一本独立纸面账（初始资金 baselines.initial_equity_usdt）：
- buy_hold：三币等权买入持有，名义合计 = 1× 初始资金，不止损。
- ma_trend：已收盘日线收盘 > MA50 做多，< MA50 做空。
- random_dir：每日每币随机方向（固定种子 + 日期 + 币，种子写入记录）。
- 消融（用当日已记录的 stances 重新算共识，零额外 LLM 成本）：tm_only、analysts_only（market/sentiment/news/flows）、
  rm_only（研究经理）、full（现行全体投票角色）；另有逐个剔除一个角色的 loo_<角色>。
除 buy_hold 外统一套用：R2 止损（ATR 倍数，夹在上下限）与 R 倍数止盈、R1 单笔风险与名义上限、R4 总止损风险上限；
持有期 = scoring.horizon_days。同一根日线同时碰到止损和止盈按止损计。只按现价入场（不模拟条件单），这是简化。
成本：开平各收 swap.fee_rate；资金费按 okx_data.funding_rate 当前值 × 名义 × 每天 3 次 × 持有天数估算（多头正费率付出）。
记录：state/baselines.jsonl（开仓/平仓/每日权益）；持仓簿 state/baselines_book.json。
"""
from __future__ import annotations

import hashlib
import math
import random

from desk import settings, sizing

LOG = settings.STATE / "baselines.jsonl"
BOOK = settings.STATE / "baselines_book.json"
ANALYSTS = ("market", "sentiment", "news", "flows")


def bcfg(cfg) -> dict:
    d = {"enabled": True, "initial_equity_usdt": 10000.0, "seed": 20261023, "leave_one_out": True}
    d.update(cfg.get("baselines") or {})
    return d


def subsets(voters: list, loo: bool = True) -> dict:
    out = {"tm_only": ["trade_manager"], "analysts_only": list(ANALYSTS), "rm_only": ["research_manager"], "full": list(voters)}
    if loo:
        for r in voters:
            out[f"loo_{r}"] = [v for v in voters if v != r]
    return out


def ablation_side(rec: dict, roles: list, th: float) -> str | None:
    """用决策记录里的 stances 与 weights 只取 roles 子集重新算共识 S（同 manager.consensus，含 L3 无效票排除）。"""
    from desk.manager import DIR_NUM
    from desk.stances import INVALID_STATUS, stance_status
    st, w = rec.get("stances") or {}, rec.get("weights") or {}
    rs = [r for r in roles if r in st and stance_status(st[r]) not in INVALID_STATUS]
    den = sum(float(w.get(r, 1.0)) for r in rs)
    if not den:
        return None
    S = sum(float(w.get(r, 1.0)) * DIR_NUM.get(st[r].get("direction"), 0) * float(st[r].get("confidence") or 0) for r in rs) / den
    return "long" if S > th else ("short" if S < -th else None)


def ma_side(df) -> str | None:
    c = df.Close
    if len(c) < 50:
        return None
    ma = float(c.tail(50).mean())
    return "long" if float(c.iloc[-1]) > ma else ("short" if float(c.iloc[-1]) < ma else None)


def random_side(seed: int, date: str, inst: str) -> str:
    h = int(hashlib.sha256(f"{seed}:{date}:{inst}".encode()).hexdigest()[:12], 16)
    return random.Random(h).choice(["long", "short"])


def _atr(df) -> float:
    h, l, c = df.High, df.Low, df.Close
    tr = (h - l).combine((h - c.shift()).abs(), max).combine((l - c.shift()).abs(), max)
    return float(tr.tail(14).mean())


def _equity(book: dict, name: str, init: float) -> float:
    return init + float(book.setdefault(name, {"realized": 0.0, "open": {}, "trades": []})["realized"])


def _open(cfg, book, name, inst, side, px, atr, date, init, extra=None) -> dict | None:
    rc = sizing.rcfg(cfg)
    b = book.setdefault(name, {"realized": 0.0, "open": {}, "trades": []})
    if inst in b["open"] or not side or not atr or px <= 0:
        return None
    eq = _equity(book, name, init)
    stop, tp, _ = sizing.stop_take(side, px, atr, rc)
    dist = abs(px - stop) / px + float(rc["cost_buffer_pct"])
    risk = eq * float(rc["risk_per_trade_pct"])
    notional = min(risk / dist, eq * float(rc["max_notional_per_trade_pct"]))
    open_risk = sum(p["notional"] * p["dist"] for p in b["open"].values())
    room = eq * float(rc["max_open_risk_pct"]) - open_risk          # R4：总止损风险上限
    if room <= 0:
        return None
    notional = min(notional, room / dist)
    fee = notional * float(cfg.get("swap", {}).get("fee_rate", 0.0005))
    pos = {"side": side, "entry": px, "stop": stop, "tp": tp, "notional": notional, "dist": dist, "date": date,
           "risk_usd": notional * dist, "fee_open": fee, **(extra or {})}
    b["open"][inst] = pos
    return {"kind": "open", "group": name, "inst_id": inst, **pos}


def _close(cfg, book, name, inst, px, date, why, funding_rate=0.0) -> dict:
    b = book[name]
    p = b["open"].pop(inst)
    sg = 1 if p["side"] == "long" else -1
    gross = sg * (px / p["entry"] - 1) * p["notional"]
    fee = p["fee_open"] + p["notional"] * float(cfg.get("swap", {}).get("fee_rate", 0.0005))
    days = max(1, (_d(date) - _d(p["date"])).days)
    fund = sg * float(funding_rate) * p["notional"] * 3 * days
    pnl = gross - fee - fund
    b["realized"] += pnl
    r_mult = pnl / p["risk_usd"] if p.get("risk_usd") else None
    t = {"inst_id": inst, "side": p["side"], "open": p["date"], "close": date, "pnl": pnl, "r": r_mult, "why": why}
    b["trades"].append(t)
    return {"kind": "close", "group": name, **t, "fee": fee, "funding": fund}


def _d(s):
    import datetime as _dt
    return _dt.date.fromisoformat(str(s)[:10])


def _walk(cfg, book, name, inst, df, date, funding, horizon) -> list:
    """用开仓日之后、到今天为止的已收盘日线检查止损/止盈/到期。"""
    p = book[name]["open"].get(inst)
    if not p or "stop" not in p or p.get("no_stop"):
        return []
    bars = df[df.Date.dt.date > _d(p["date"])]
    for _, b in bars.iterrows():
        bd = str(b.Date.date())
        if p["side"] == "long":
            if b.Low <= p["stop"]:
                return [_close(cfg, book, name, inst, p["stop"], bd, "止损", funding)]
            if b.High >= p["tp"]:
                return [_close(cfg, book, name, inst, p["tp"], bd, "止盈", funding)]
        else:
            if b.High >= p["stop"]:
                return [_close(cfg, book, name, inst, p["stop"], bd, "止损", funding)]
            if b.Low <= p["tp"]:
                return [_close(cfg, book, name, inst, p["tp"], bd, "止盈", funding)]
        if (_d(bd) - _d(p["date"])).days >= horizon:
            return [_close(cfg, book, name, inst, float(b.Close), bd, "到期", funding)]
    return []


def update(cfg, date: str, decisions: list, candles=None, funding=None) -> list:
    """每日调用一次（同一 date 重复调用不重复开仓）。返回本次写入的记录。candles/funding 可注入（测试）。"""
    B = bcfg(cfg)
    if not B["enabled"]:
        return []
    if candles is None or funding is None:
        from desk import okx_data
        candles = candles or (lambda i: okx_data.candles(i, "1Dutc", 80))
        funding = funding or okx_data.funding_rate
    init, seed = float(B["initial_equity_usdt"]), int(B["seed"])
    horizon = int(cfg["scoring"]["horizon_days"])
    th = float(cfg["manager"].get("consensus_threshold", 0.1))
    from desk.manager import VOTERS
    groups = subsets(list(VOTERS), bool(B["leave_one_out"]))
    book = settings.read_json(BOOK, {})
    if book.get("_last_date") == date:
        return []
    out = []
    recs = {d["inst_id"]: d for d in decisions or [] if d.get("inst_id")}
    coins = [c["inst_id"] for c in cfg["coins"]]
    data, fr = {}, {}
    for inst in coins:
        try:
            data[inst] = candles(inst)
            fr[inst] = float(funding(inst) or 0)
        except Exception:
            continue
    names = ["buy_hold", "ma_trend", "random_dir"] + list(groups)
    for name in names:
        book.setdefault(name, {"realized": 0.0, "open": {}, "trades": []})
    for inst, df in data.items():
        px, atr = float(df.Close.iloc[-1]), _atr(df)
        for name in names:
            if name == "buy_hold":
                if inst not in book[name]["open"]:
                    n = init / len(coins)
                    book[name]["open"][inst] = {"side": "long", "entry": px, "notional": n, "dist": 0.0, "date": date,
                                                "risk_usd": 0.0, "no_stop": True,
                                                "fee_open": n * float(cfg.get("swap", {}).get("fee_rate", 0.0005))}
                    out.append({"kind": "open", "group": name, "inst_id": inst, "entry": px, "notional": n})
                continue
            out += _walk(cfg, book, name, inst, df, date, fr.get(inst, 0.0), horizon)
            if name == "ma_trend":
                side, extra = ma_side(df), None
            elif name == "random_dir":
                side, extra = random_side(seed, date, inst), {"seed": seed}
            else:
                rec = recs.get(inst)
                side, extra = (ablation_side(rec, groups[name], th) if rec else None), None
            o = _open(cfg, book, name, inst, side, px, atr, date, init, extra)
            if o:
                out.append(o)
    # 每日权益（含浮动）
    for name in names:
        b = book[name]
        upnl = 0.0
        for inst, p in b["open"].items():
            if inst in data:
                sg = 1 if p["side"] == "long" else -1
                upnl += sg * (float(data[inst].Close.iloc[-1]) / p["entry"] - 1) * p["notional"] - p.get("fee_open", 0)
        eq = init + b["realized"] + upnl
        b.setdefault("equity_curve", []).append([date, round(eq, 4)])
        out.append({"kind": "mark", "group": name, "equity": round(eq, 4), "open": len(b["open"])})
    book["_last_date"] = date
    settings.write_json(BOOK, book)
    for r in out:
        settings.append_jsonl(LOG, {"date": date, **r})
    return out


def stats(cfg) -> dict:
    init = float(bcfg(cfg)["initial_equity_usdt"])
    book = settings.read_json(BOOK, {})
    res = {}
    for name, b in book.items():
        if name.startswith("_"):
            continue
        curve = [e for _, e in b.get("equity_curve", [])] or [init]
        peak, mdd = -math.inf, 0.0
        for e in curve:
            peak = max(peak, e)
            mdd = max(mdd, 1 - e / peak if peak > 0 else 0)
        tr = b.get("trades", [])
        rs = [t["r"] for t in tr if t.get("r") is not None]
        res[name] = {"ret": curve[-1] / init - 1, "mdd": mdd, "n": len(tr),
                     "win": (sum(1 for t in tr if t["pnl"] > 0) / len(tr)) if tr else None,
                     "avg_r": (sum(rs) / len(rs)) if rs else None, "open": len(b.get("open", {}))}
    return res


def report_lines(cfg) -> list:
    s = stats(cfg)
    if not s:
        return ["## 对照组（B1，纸面）", "", "暂无记录。", ""]
    L = ["## 对照组（B1，纸面记账，不下单）", "",
         f"每组独立纸面资金 {bcfg(cfg)['initial_equity_usdt']:g} USDT；除买入持有外统一用 R1/R2/R4 定仓与止损、只按现价入场；含双边手续费与资金费估算。"
         "消融组用当日已记录的各角色观点重新计算共识，不额外调用模型。", "",
         "| 对照组 | 累计收益 | 最大回撤 | 交易数 | 胜率 | 平均 R | 在持 |", "|---|---|---|---|---|---|---|"]
    order = ["full", "buy_hold", "ma_trend", "random_dir", "tm_only", "analysts_only", "rm_only"]
    for name in order + sorted(k for k in s if k not in order):
        if name not in s:
            continue
        v = s[name]
        L.append(f"| {name} | {v['ret']:+.2%} | {v['mdd']:.2%} | {v['n']} | "
                 + (f"{v['win']:.0%}" if v["win"] is not None else "无") + " | "
                 + (f"{v['avg_r']:+.2f}" if v["avg_r"] is not None else "无") + f" | {v['open']} |")
    L.append("")
    return L
