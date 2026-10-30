"""R1–R4 统一定仓（纯函数，无网络）。所有开仓路径（open_now / place_trigger / 限价补单的上限）与日报都只用这里的结果。

R2 入场方案：同向角色按入场条件分为 now / breakout / pullback 三组，按组内 Σ w×conf 选组，入场价只在组内加权；
   两组得分差 < plan_conflict_pct（20%）→ 方案冲突不开仓（plan_conflict）。止损 = clip(stop_atr_mult×ATR14/entry, stop_min, stop_max)，
   止盈 = 入场 ± tp_r_multiple × 止损距离。再按同向角色 stop/target 做 w×置信度加权调整；调整后盈亏比 RR=止盈距/止损距（相对入场）不得低于 min_rr_after_adjust。
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
                 "max_price_drift_atr": 0.5, "max_daily_bar_age_days": 2, "min_rr_after_adjust": 1.5}

SIZING_DEFAULTS = {"no_entry_not_now_vote": True, "pending_action_field": True, "same_px_tol_pct": 0.05,
                   # 2026-10-30 止损止盈锚定修正（feat/sltp-anchor-fix），false=旧行为
                   "sltp_anchor_entry": True, "sltp_anchor_max_mult": 1.25,
                   "sltp_rr_tighten_stop_first": True,
                   "sltp_tp_cap": True, "sltp_tp_cap_mult": 1.2,
                   "sltp_exclude_self_levels": True}


def anchor_opts(cfg, rc: dict | None = None, atr=None, entry=None) -> dict:
    """finalize_sl_tp 的新规则参数（按 sizing 段开关）。base_dist=stop_take 的基准止损距离（价格单位）。"""
    sc = scfg(cfg)
    rc = rc or rcfg(cfg)
    base = None
    if atr and entry:
        _s, _t, d = stop_take("long", float(entry), float(atr), rc)
        base = d * float(entry)
    return {"base_dist": base,
            "anchor_mult": float(sc["sltp_anchor_max_mult"]) if sc["sltp_anchor_entry"] else None,
            "tighten_first": bool(sc["sltp_rr_tighten_stop_first"]),
            "tp_cap_mult": float(sc["sltp_tp_cap_mult"]) if sc["sltp_tp_cap"] else None,
            "tp_r": float(rc["tp_r_multiple"])}


def drop_self_levels(st: dict, pos: dict | None, tol_pct: float = 0.05) -> tuple[dict, list]:
    """规则 4：对已有持仓调点加权时，角色 target/stop 与现有持仓止盈/止损同价（容差 same_px_tol_pct）→ 排除该项，
    避免把现有点位原样回收进加权（自我强化）。返回 (新立场字典, 被排除的 [(角色, 字段, 值)])。"""
    out, dropped = {}, []
    tp, sl = (pos or {}).get("tp_px"), (pos or {}).get("sl_px")
    for r, s in (st or {}).items():
        s2 = dict(s or {})
        for f, ref in (("target", tp), ("stop", sl)):
            v = s2.get(f)
            if v not in (None, "") and ref not in (None, "") and same_px(v, ref, tol_pct):
                dropped.append((r, f, v)); s2[f] = None
        out[r] = s2
    return out, dropped


def scfg(cfg) -> dict:
    """sizing 段开关（2026-10-30 新增）：no_entry_not_now_vote（改动 1）、pending_action_field（改动 2）默认 true。"""
    return {**SIZING_DEFAULTS, **((cfg or {}).get("sizing") or {})}


def rcfg(cfg) -> dict:
    return {**RISK_DEFAULTS, **(cfg.get("risk") or {})}


# ------------------------------------------------------------------ R2
def stop_take(side: str, entry: float, atr: float, rc: dict) -> tuple[float, float, float]:
    """返回 (止损价, 止盈价, 止损距离比例)。"""
    d = min(max(float(rc["stop_atr_mult"]) * float(atr) / float(entry), float(rc["stop_min_pct"])), float(rc["stop_max_pct"]))
    sg = 1 if side == "long" else -1
    return entry * (1 - sg * d), entry * (1 + sg * float(rc["tp_r_multiple"]) * d), d



def rr_ratio(side: str, entry: float, stop, tp) -> float | None:
    """盈亏比 RR = |止盈−入场| / |入场−止损|（相对入场价）。侧向不对或距离≤0 → None。"""
    if not entry or stop is None or tp is None:
        return None
    try:
        entry, stop, tp = float(entry), float(stop), float(tp)
    except (TypeError, ValueError):
        return None
    sg = 1 if side == "long" else -1
    sl_dist = sg * (entry - stop)
    tp_dist = sg * (tp - entry)
    if sl_dist <= 0 or tp_dist <= 0:
        return None
    return tp_dist / sl_dist


def wavg_levels(st: dict, w: dict, roles: list, side: str, entry: float) -> dict:
    """同向/给定角色的 stop、target 按 w×置信度加权。只取相对入场正确一侧。返回 {stop, target, stop_n, target_n}。"""
    sg = 1 if side == "long" else -1

    def avg(field: str, ok) -> tuple:
        vals = []
        for r in roles:
            v = (st.get(r) or {}).get(field)
            if v is None or v == "":
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if not ok(fv):
                continue
            a = float(w.get(r, 0) or 0) * float((st.get(r) or {}).get("confidence") or 0)
            if a > 0:
                vals.append((a, fv))
        den = sum(a for a, _ in vals)
        return (sum(a * v for a, v in vals) / den if den else None), len(vals)

    stop, sn = avg("stop", lambda v: sg * (entry - v) > 0)
    target, tn = avg("target", lambda v: sg * (v - entry) > 0)
    return {"stop": stop, "target": target, "stop_n": sn, "target_n": tn}


def finalize_sl_tp(side: str, entry: float, base_stop, base_tp, role_stop=None, role_tp=None, min_rr: float = 1.5,
                   base_dist=None, anchor_mult=None, tighten_first: bool = False, tp_cap_mult=None, tp_r: float = 2.0) -> dict:
    """ATR(+2R) 基准 → 角色加权覆盖 → 强制 RR≥min_rr（不够则外推止盈；组合无效则回退基准）。
    2026-10-30 新规则（参数均缺省=旧行为，由 anchor_opts 按开关给出；base_dist=基准止损距离，价格单位）：
      anchor_mult：止损相对 entry 的距离 > anchor_mult×base_dist → 收紧到该上限（只收紧）；
      tp_cap_mult：止盈距离 ≤ tp_cap_mult×tp_r×base_dist（封顶）；
      tighten_first：RR 不足时先把止损收紧到基准距离（只收紧），再按需要外推止盈（不超过封顶）；仍不足如实记录。
    返回 {stop, take_profit, rr, adjusted, notes, rr_short}。"""
    notes: list[str] = []
    stop = role_stop if role_stop is not None else base_stop
    tp = role_tp if role_tp is not None else base_tp
    adjusted = role_stop is not None or role_tp is not None
    if role_stop is not None:
        notes.append(f"止损按角色加权调整→{float(role_stop):.6g}")
    if role_tp is not None:
        notes.append(f"止盈按角色加权调整→{float(role_tp):.6g}")
    sg = 1 if side == "long" else -1
    e = float(entry)
    bd = float(base_dist) if base_dist else None
    rr_short = None
    if bd and anchor_mult and stop is not None and sg * (e - float(stop)) > anchor_mult * bd + 1e-9:
        ns = e - sg * anchor_mult * bd
        notes.append(f"止损 {float(stop):.6g} 距入场 {abs(e - float(stop)) / e:.2%} 超过基准距离 {bd / e:.2%} 的 {anchor_mult:g} 倍，"
                     f"按实际入场价 {e:.6g} 收紧至 {ns:.6g}")
        stop, adjusted = ns, True
    cap = tp_cap_mult * tp_r * bd if (bd and tp_cap_mult) else None
    if cap and tp is not None and sg * (float(tp) - e) > cap + 1e-9:
        nt = e + sg * cap
        notes.append(f"止盈 {float(tp):.6g} 距入场超过基准止盈距离的 {tp_cap_mult:g} 倍，封顶至 {nt:.6g}")
        tp, adjusted = nt, True
    rr = rr_ratio(side, entry, stop, tp)
    min_rr = float(min_rr)
    if stop is not None and rr is not None and rr + 1e-12 < min_rr:
        if tighten_first and bd and sg * (e - float(stop)) > bd + 1e-9:
            ns = e - sg * bd
            notes.append(f"调整后 RR={rr:.3f}<{min_rr:g}，先把止损收紧到基准距离 {ns:.6g}")
            stop, adjusted = ns, True
            rr = rr_ratio(side, entry, stop, tp)
        if rr is not None and rr + 1e-12 < min_rr:
            new_tp = e + sg * min_rr * abs(e - float(stop))
            if cap and sg * (new_tp - e) > cap + 1e-9:
                new_tp = e + sg * cap
                rr2 = rr_ratio(side, entry, stop, new_tp)
                notes.append(f"调整后 RR={rr:.3f}<{min_rr:g}，止盈外推受封顶限制至 {new_tp:.6g}，RR 仍为 {rr2:.3f}<{min_rr:g}（如实记录）")
                rr_short = rr2
                tp, rr = new_tp, rr2
            else:
                notes.append(f"调整后 RR={rr:.3f}<{min_rr:g}，止盈外推至 {new_tp:.6g} 以满足 RR≥{min_rr:g}")
                tp, rr = new_tp, min_rr
            adjusted = True
    elif adjusted and rr is None and base_stop is not None:
        notes.append("调整后点位无效（侧向/距离），回退 ATR 基准")
        stop, tp = base_stop, base_tp
        rr = rr_ratio(side, entry, stop, tp)
        adjusted = False
    return {"stop": stop, "take_profit": tp, "rr": rr, "adjusted": adjusted, "notes": notes, "rr_short": rr_short}


def guard_rr(side: str, entry: float, stop, tp, min_rr: float = 1.5) -> tuple[bool, str, float | None]:
    """已有持仓调点护栏：能算出 RR 时必须 ≥ min_rr。缺止盈时不拦（只改止损场景）。"""
    if tp is None or stop is None or not entry:
        return True, "", None
    rr = rr_ratio(side, entry, stop, tp)
    if rr is None:
        return False, "点位侧向无效，无法计算盈亏比", None
    if rr + 1e-12 < float(min_rr):
        return False, f"调整后盈亏比 RR={rr:.3f} < {float(min_rr):g}", rr
    return True, "", rr


def classify(side: str, cond: str | None, entry: float | None, last: float, band: float) -> str:

    if cond == "now" or not entry or abs(float(entry) / last - 1) <= band:
        return "now"
    up = float(entry) > last
    return "breakout" if (up if side == "long" else not up) else "pullback"


PENDING_ACTIONS = ("keep", "reprice", "cancel", "now")   # 立场行「对现有挂单」：保留/改价/撤销/改现价


def same_px(a, b, tol_pct: float = 0.05) -> bool:
    """两个价位视为同价（默认相差 ≤0.05%）。"""
    try:
        return abs(float(a) / float(b) - 1) * 100 <= tol_pct
    except (TypeError, ValueError, ZeroDivisionError):
        return False


def choose_plan(st: dict, w: dict, roles: list, side: str, last: float, band: float, conflict_pct: float = 0.20,
                no_entry_not_now: bool = False, existing: dict | None = None) -> dict:
    """roles=同向角色。返回 {group, entry, condition, horizon, scores, conflict, ...}。

    no_entry_not_now（sizing.no_entry_not_now_vote）：没写入场价、也没明确写现价的角色只算方向票，不进 now 组；
      若所有同向角色都落到这一类（一个入场方案票都没有），回退旧行为：这些角色计入 now 组（现价入场）。
    existing（stances.pending_keep_default 开启时由调用方传入的同向现有挂单 {trigger_px, condition}）：
      没写入场价、也没写「对现有挂单」的角色按「保留」处理，以现有挂单触发价作隐含入场价参与分组；
      pending_action=keep 以现有挂单为准；reprice 以其入场价为准；cancel 不投入场票；now 计入 now 组。
      隐含入场价只用于分组，不写回立场（打分仍按原立场的方向/目标/止损）。"""
    groups: dict[str, list] = {"now": [], "breakout": [], "pullback": []}
    eff: dict[str, float] = {}
    dir_only, implied, cancelled = [], [], []
    ex_px = float(existing["trigger_px"]) if existing and existing.get("trigger_px") else None
    for r in roles:
        s = st[r]
        pa = s.get("pending_action")
        e, cond = s.get("entry_px"), s.get("entry_condition")
        if pa == "now":
            cond = "now"
        elif pa == "cancel" and ex_px:
            cancelled.append(r); continue
        elif ex_px and cond != "now" and (pa == "keep" or (not e and pa is None)):
            e, cond = ex_px, existing.get("condition"); implied.append(r)
        if e and not (0.5 * last < float(e) < 1.5 * last):   # 离谱价位剔除（沿用原规则）
            continue
        if not e and cond != "now" and no_entry_not_now:
            dir_only.append(r); continue
        g = classify(side, cond, e, last, band)
        groups[g].append(r)
        if e:
            eff[r] = float(e)
    fallback = False
    if no_entry_not_now and dir_only and not any(groups.values()):
        groups["now"] = list(dir_only); dir_only = []; fallback = True   # 全部没写价：保持旧行为（现价入场）
    score = {g: sum(float(w[r]) * float(st[r].get("confidence") or 0) for r in rs) for g, rs in groups.items()}
    ranked = sorted(score.items(), key=lambda x: -x[1])
    top, sec = ranked[0], ranked[1]
    out = {"scores": {k: round(v, 4) for k, v in score.items()}, "group": top[0], "conflict": False,
           "direction_only": dir_only, "implied_keep": implied, "cancel_votes": cancelled, "no_entry_fallback_now": fallback,
           "members": {g: list(rs) for g, rs in groups.items()}}
    if top[1] <= 0:
        return {**out, "group": None}
    if sec[1] > 0 and (top[1] - sec[1]) / top[1] < conflict_pct:
        return {**out, "conflict": True}
    rs = groups[top[0]]
    wsum = sum(float(w[r]) * float(st[r].get("confidence") or 0) for r in rs)
    if top[0] == "now":
        entry = last
    else:
        entry = sum(float(w[r]) * float(st[r]["confidence"]) * eff[r] for r in rs) / wsum
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


def pending_keep_sltp(side: str, trigger: float, old_sl, old_tp, new_sl, new_tp, now_px: float,
                      min_rr: float = 1.5, gap_pct: float = 0.3) -> dict:
    """保留同价位现有条件单时，按交易经理给的 new 止损/止盈改点（2026-10-30 用户规则，纯函数）：
    只给一项另一项保持原值；止损只收紧（做空只下移、做多只上移）且须在触发价亏损一侧；
    止盈须在触发价盈利一侧且距现价至少 gap_pct%；按触发价算 RR≥min_rr；违规的项保持原值并记原因。"""
    sg = 1 if side == "long" else -1
    sl, tp, why = old_sl, old_tp, []
    if new_sl is not None and old_sl is not None and abs(float(new_sl) - float(old_sl)) > 1e-12:
        if sg * (float(trigger) - float(new_sl)) <= 0:
            why.append(f"新止损 {float(new_sl):g} 不在触发价亏损一侧，保持原止损 {float(old_sl):g}")
        elif sg * (float(new_sl) - float(old_sl)) < 0:
            why.append(f"新止损 {float(new_sl):g} 比原止损 {float(old_sl):g} 放宽（只允许收紧），保持原止损")
        else:
            sl = float(new_sl)
    elif new_sl is not None and old_sl is None:
        sl = float(new_sl)
    if new_tp is not None and (old_tp is None or abs(float(new_tp) - float(old_tp)) > 1e-12):
        if sg * (float(new_tp) - float(trigger)) <= 0:
            why.append(f"新止盈 {float(new_tp):g} 不在触发价盈利一侧，保持原止盈 {old_tp}")
        elif abs(float(new_tp) / float(now_px) - 1) * 100 < float(gap_pct):
            why.append(f"新止盈 {float(new_tp):g} 距现价 {float(now_px):g} 不足 {float(gap_pct):g}%，保持原止盈 {old_tp}")
        else:
            tp = float(new_tp)
    if (sl, tp) != (old_sl, old_tp) and sl is not None and tp is not None:
        rr = rr_ratio(side, float(trigger), sl, tp)
        if rr is None or rr + 1e-12 < float(min_rr):
            why.append(f"调整后 RR={'无效' if rr is None else f'{rr:.2f}'} < {float(min_rr):g}，止损止盈都保持原值")
            sl, tp = old_sl, old_tp
    return {"stop_loss": sl, "take_profit": tp, "changed": (sl, tp) != (old_sl, old_tp), "reasons": why,
            "rr": rr_ratio(side, float(trigger), sl, tp) if sl is not None and tp is not None else None}
