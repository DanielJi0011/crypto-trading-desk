"""R1–R4 统一定仓（纯函数，无网络）。所有开仓路径（open_now / place_trigger / 限价补单的上限）与日报都只用这里的结果。

R2 入场方案：同向角色按入场条件分为 now / breakout / pullback 三组，按组内 Σ w×conf 选组，入场价只在组内加权；
   两组得分差 < plan_conflict_pct（20%）→ 方案冲突不开仓（plan_conflict）。止损 = clip(stop_atr_mult×ATR14/entry, stop_min, stop_max)，
   止盈 = 入场 ± tp_r_multiple × 止损距离。角色自己的止损/目标只作参考。
R1 定仓：risk_usd = 权益×risk_per_trade_pct×scale×dd_scale×tm_risk_scale；notional = min(risk_usd/stop_dist, 权益×max_notional, R4 剩余)；
   杠杆取满足保证金上限的最小值（步长 0.5，1 ≤ lev ≤ 新开仓上限与交易经理上限）；不够则按保证金上限反推削减 notional。
R3 强平缓冲：|强平价 − 止损| ≥ liq_buffer_atr_mult × ATR14，不满足每次降 0.5 倍；1 倍仍不满足 → liq_buffer_reject。
R4 组合层：同向净名义 ≤ max_net_directional_pct×权益；在途风险 Σ名义×止损距离 ≤ max_open_risk_pct×权益（无止损按 stop_max_pct）。
"""
from __future__ import annotations

import math

RISK_DEFAULTS = {"risk_per_trade_pct": 0.0075, "max_notional_per_trade_pct": 1.0, "max_margin_pct": 0.20, "stop_atr_mult": 1.5,
                 "stop_min_pct": 0.01, "stop_max_pct": 0.08, "tp_r_multiple": 2.0, "liq_buffer_atr_mult": 1.0,
                 "max_open_risk_pct": 0.02, "max_net_directional_pct": 1.2, "cost_buffer_pct": 0.002, "plan_conflict_pct": 0.20,
                 "max_price_drift_atr": 0.5, "max_daily_bar_age_days": 2}


def rcfg(cfg) -> dict:
    return {**RISK_DEFAULTS, **(cfg.get("risk") or {})}


# ------------------------------------------------------------------ R2
def stop_take(side: str, entry: float, atr: float, rc: dict) -> tuple[float, float, float]:
    """返回 (止损价, 止盈价, 止损距离比例)。"""
    d = min(max(float(rc["stop_atr_mult"]) * float(atr) / float(entry), float(rc["stop_min_pct"])), float(rc["stop_max_pct"]))
    sg = 1 if side == "long" else -1
    return entry * (1 - sg * d), entry * (1 + sg * float(rc["tp_r_multiple"]) * d), d


def classify(side: str, cond: str | None, entry: float | None, last: float, band: float) -> str:
    if cond == "now" or not entry or abs(float(entry) / last - 1) <= band:
        return "now"
    up = float(entry) > last
    return "breakout" if (up if side == "long" else not up) else "pullback"


def choose_plan(st: dict, w: dict, roles: list, side: str, last: float, band: float, conflict_pct: float = 0.20) -> dict:
    """roles=同向角色。返回 {group, entry, condition, horizon, scores, conflict}。"""
    groups: dict[str, list] = {"now": [], "breakout": [], "pullback": []}
    for r in roles:
        e = st[r].get("entry_px")
        if e and not (0.5 * last < float(e) < 1.5 * last):   # 离谱价位剔除（沿用原规则）
            continue
        g = classify(side, st[r].get("entry_condition"), e, last, band)
        groups[g].append(r)
    score = {g: sum(float(w[r]) * float(st[r].get("confidence") or 0) for r in rs) for g, rs in groups.items()}
    ranked = sorted(score.items(), key=lambda x: -x[1])
    top, sec = ranked[0], ranked[1]
    out = {"scores": {k: round(v, 4) for k, v in score.items()}, "group": top[0], "conflict": False}
    if top[1] <= 0:
        return {**out, "group": None}
    if sec[1] > 0 and (top[1] - sec[1]) / top[1] < conflict_pct:
        return {**out, "conflict": True}
    rs = groups[top[0]]
    wsum = sum(float(w[r]) * float(st[r].get("confidence") or 0) for r in rs)
    if top[0] == "now":
        entry = last
    else:
        entry = sum(float(w[r]) * float(st[r]["confidence"]) * float(st[r]["entry_px"]) for r in rs) / wsum
    hz = sum(float(w[r]) * float(st[r]["confidence"]) * float(st[r].get("horizon_days") or 0) for r in rs) / wsum
    cond = "now" if (top[0] == "now" or abs(entry / last - 1) <= band) else ("gte" if entry > last else "lte")
    return {**out, "entry": last if cond == "now" else entry, "condition": cond, "horizon": hz}


# ------------------------------------------------------------------ R3
def liq_price(side: str, px: float, lev: float, mmr: float) -> float:
    return px * (1 - 1 / lev + mmr) if side == "long" else px * (1 + 1 / lev - mmr)


# ------------------------------------------------------------------ R4
def book_exposure(bk: dict, rc: dict) -> dict:
    """组合层视图：多/空名义（持仓 + 条件单按触发价 + 在途入场子单）与在途风险。"""
    longs = shorts = risk = 0.0
    smax = float(rc["stop_max_pct"])
    for p in bk.get("positions") or []:
        n = float(p.get("notional") or 0)
        px, sl = float(p.get("price") or p.get("avg_px") or 0), p.get("sl_px")
        d = abs(px - float(sl)) / px if (sl and px) else smax
        risk += n * d
        if p.get("side") == "long":
            longs += n
        else:
            shorts += n
    for t in bk.get("trigger_risk") or []:
        risk += t["stop_risk"]
        if t["side"] == "long":
            longs += t["notional"]
        else:
            shorts += t["notional"]
    for t in bk.get("pending_entries") or []:
        risk += t["stop_risk"]
        if t["side"] == "long":
            longs += t["notional"]
        else:
            shorts += t["notional"]
    return {"long": longs, "short": shorts, "net": longs - shorts, "open_risk": risk}


def headroom(rc: dict, equity: float, ex: dict, side: str, stop_dist: float) -> tuple[float, str]:
    """R4 剩余名义额度与约束名。"""
    net_cap = float(rc["max_net_directional_pct"]) * equity
    net_room = net_cap - ex["net"] if side == "long" else net_cap + ex["net"]
    risk_room = float(rc["max_open_risk_pct"]) * equity - ex["open_risk"]
    risk_notional = risk_room / stop_dist if stop_dist > 0 else 0.0
    if net_room <= risk_notional:
        return max(net_room, 0.0), "max_net_directional_pct"
    return max(risk_notional, 0.0), "max_open_risk_pct"


# ------------------------------------------------------------------ R1
def size(rc: dict, *, equity: float, S: float, entry: float, stop: float, side: str, full_size: float, half_below: float,
         lev_cap: float, mmr: float, atr: float, drawdown: float = 0.0, max_drawdown: float = 0.10, tm_risk_scale: float = 1.0,
         tm_lev_cap: float | None = None, room: float | None = None, room_limit: str | None = None, min_order_usd: float = 0.0) -> dict:
    """返回 {ok, reason, risk_usd, notional, leverage, margin, stop_loss_usd, stop_dist, clipped:[...]}。"""
    scale = min(1.0, abs(S) / full_size) * (0.5 if abs(S) < half_below else 1.0)
    dd_scale = min(max(1 - drawdown / max_drawdown, 0.25), 1.0) if max_drawdown > 0 else 1.0
    tm = min(max(float(tm_risk_scale), 0.0), 1.0)   # 只降不升
    risk_usd = equity * float(rc["risk_per_trade_pct"]) * scale * dd_scale * tm
    stop_dist = abs(entry - stop) / entry + float(rc["cost_buffer_pct"])
    clipped = []
    raw = risk_usd / stop_dist
    notional = min(raw, equity * float(rc["max_notional_per_trade_pct"]))
    if notional < raw - 1e-9:
        clipped.append({"limit": "max_notional_per_trade_pct", "before": raw, "after": notional})
    if room is not None and notional > room:
        clipped.append({"limit": room_limit or "R4", "before": notional, "after": room})
        notional = room
    cap = float(lev_cap)
    if tm_lev_cap:
        cap = min(cap, max(float(tm_lev_cap), 1.0))
    mcap = equity * float(rc["max_margin_pct"])

    def fit(cap_now):
        need = notional / mcap if mcap > 0 else math.inf
        lev = max(1.0, math.ceil(need * 2 - 1e-9) / 2)
        if lev > cap_now:
            lev = math.floor(cap_now * 2 + 1e-9) / 2
        return max(lev, 1.0)
    lev = fit(cap)
    # R3：强平缓冲
    while True:
        liq = liq_price(side, entry, lev, mmr)
        if abs(liq - stop) >= float(rc["liq_buffer_atr_mult"]) * atr - 1e-9 and ((side == "long" and stop > liq) or (side == "short" and stop < liq)):
            break
        if lev <= 1.0:
            return {"ok": False, "reason": "liq_buffer_reject", "risk_usd": risk_usd, "notional": 0.0, "leverage": 1.0,
                    "margin": 0.0, "stop_loss_usd": 0.0, "stop_dist": stop_dist, "clipped": clipped, "liq_px": liq}
        lev -= 0.5
    if notional / lev > mcap + 1e-9:
        clipped.append({"limit": "max_margin_pct", "before": notional, "after": mcap * lev})
        notional = mcap * lev
    out = {"ok": True, "reason": None, "risk_usd": risk_usd, "notional": notional, "leverage": lev, "margin": notional / lev,
           "stop_loss_usd": notional * abs(entry - stop) / entry, "stop_dist": stop_dist, "clipped": clipped,
           "liq_px": liq_price(side, entry, lev, mmr), "scale": scale, "dd_scale": dd_scale}
    if notional < max(min_order_usd, 1e-9):
        out.update(ok=False, reason="size_too_small")
    return out
