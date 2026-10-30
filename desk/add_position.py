"""同向加仓规则（risk.allow_add_position，默认 true；false = 恢复旧的「每币一仓不加仓」）。

加仓当成一笔新订单：按加仓入场价独立算止盈止损（ATR+2R + 全员加权，RR≥min_rr），
只覆盖加仓张数；原仓的止盈止损与张数完全不动，不撤原仓 reduceOnly 保护单。

2026-10-30 用户决定（add_position.tm_decides_add，默认 true）：是否加仓由交易经理决定，下面的预算只作参考不否决，
加仓只受硬护栏（合并保证金、总名义、R4、止损、当日亏损、KILL）约束；false 恢复下述旧预算硬拒。
风险预算（旧行为）：原仓止损风险（各已开腿之和；无腿登记时退回「现止损到均价 × 张数，盈利区按 0」）
+ 本笔加仓止损风险 ≤ 单笔上限（0.75%×权益×共识/回撤/交易经理缩放）。
预算不足则削减加仓量；剩余低于最小下单量 → 不加仓（add_budget_full）。

反向不反手；旧纸面仓不加仓。条件加仓单成交后复核预算（超则只减加仓部分），
再确认本腿保护单存在；绝不 consolidate / 撤旧保护单。

本模块计算函数是纯函数（无网络）；执行在 demo_exec.add_now / place_add_trigger 与 on_fill。
"""
from __future__ import annotations


def _sg(side: str) -> int:
    return 1 if side == "long" else -1


def enabled(cfg) -> bool:
    return bool((cfg.get("risk") or {}).get("allow_add_position", True))


def tm_decides(cfg) -> bool:
    """add_position.tm_decides_add（默认 true，2026-10-30 用户决定）：是否加仓由交易经理权衡后决定。
    true：加仓风险预算（min(risk_usd, 权益×risk_per_trade_pct) − 现有风险）只作给交易经理的参考数据，不削减、不否决；
    加仓只受硬护栏约束（合并后单笔保证金 max_position_pct、总名义 max_total_notional_pct、R4 在途风险/同向净名义、
    必须有止损、当日亏损停开仓、KILL/HALT）。交易经理仍可否决；交易经理无结论仍不加仓（manager.tm_required_for_open）。
    false：恢复旧行为（预算不足削减，预算满 add_budget_full 拒绝）。"""
    return bool((cfg.get("add_position") or {}).get("tm_decides_add", True))


def existing_risk(pos: dict, rc: dict, legs: list | None = None) -> float:
    """原仓（已开各腿）止损风险 USDT。有 legs 时按腿加总；否则：止损相对均价，盈利区按 0；无止损按 stop_max_pct。"""
    if legs:
        tot = 0.0
        for lg in legs:
            sl, entry, qty = lg.get("sl"), lg.get("entry"), lg.get("qty")
            if sl in (None, "") or entry in (None, "") or not qty:
                continue
            tot += max(0.0, _sg(lg.get("side") or pos["side"]) * (float(entry) - float(sl))) * float(qty)
        return tot
    qty, avg = float(pos.get("qty") or 0), float(pos.get("avg_px") or 0)
    sl = pos.get("sl_px")
    if sl in (None, ""):
        return qty * avg * float(rc.get("stop_max_pct") or 0.08)
    return max(0.0, _sg(pos["side"]) * (avg - float(sl))) * qty


def new_avg(pos: dict, add_qty: float, add_px: float) -> float:
    """加仓后交易所均价（仅用于强平等护栏估算；本规则不再用它算止盈止损）。"""
    q0 = float(pos.get("qty") or 0)
    return (q0 * float(pos["avg_px"]) + float(add_qty) * float(add_px)) / (q0 + float(add_qty)) if (q0 + float(add_qty)) > 0 else float(add_px)


def budget(rc: dict, pos: dict, add_px: float, add_sl: float, sz_info: dict, equity: float,
           max_position_pct: float, min_order_usd: float, legs: list | None = None,
           tm_decides: bool = False, total_room: float | None = None, max_total_pct: float | None = None,
           total_now: float | None = None) -> dict:
    """在 sizing.size 结果上算加仓张数。dist 用加仓入场价到本笔止损。
    预算（cap = min(risk_usd, 权益×risk_per_trade_pct)，existing_risk，budget_left）始终计算并输出。
    tm_decides=False（旧行为）：按预算削减，预算满 → add_budget_full 拒绝。
    tm_decides=True（2026-10-30 用户决定）：预算只作参考（over_budget 标记），不削减不拒绝；
    只按硬护栏削减：合并后保证金 ≤ max_position_pct%、总名义剩余额度 total_room（含挂单/在途子单）、
    R4 剩余额度（sizing.size 已按 sz_info.room 削减）。hard_max_notional = 按硬护栏还能加的最大名义。"""
    side = pos["side"]
    cap = min(float(sz_info["risk_usd"]), float(equity) * float(rc["risk_per_trade_pct"]))
    ex = existing_risk(pos, rc, legs)
    left = cap - ex
    info = {"existing_risk": round(ex, 4), "cap": round(cap, 4), "stop_ref": add_sl, "base_sl": pos.get("sl_px"),
            "clipped": False, "clip_by": [], "notional_want": round(float(sz_info.get("notional") or 0), 4),
            "tm_decides": bool(tm_decides)}
    if add_sl in (None, ""):
        return {**info, "ok": False, "reason": "no_stop", "notional": 0.0, "add_risk": 0.0, "budget_left": round(left, 4)}
    if _sg(side) * (float(add_px) - float(add_sl)) <= 0:
        return {**info, "ok": False, "reason": "add_px_beyond_stop", "notional": 0.0, "add_risk": 0.0, "budget_left": round(left, 4)}
    dist = abs(float(add_px) - float(add_sl)) / float(add_px) + float(rc["cost_buffer_pct"])
    want = float(sz_info.get("notional") or 0)
    by_budget = max(0.0, left) / dist
    lev = float(pos.get("leverage") or 1)
    m_room = float(equity) * float(max_position_pct) / 100 - float(pos.get("margin") or 0)
    # 硬护栏可加最大名义（不含预算）：合并保证金、总名义、R4（sz_info.room 为 R4 剩余名义）
    hard = [("max_position_pct(合并保证金)", max(0.0, m_room * lev))]
    if total_room is not None:
        hard.append(("max_total_notional_pct", max(0.0, float(total_room))))
    if sz_info.get("room") is not None:
        hard.append((str(sz_info.get("room_limit") or "R4"), max(0.0, float(sz_info["room"]))))
    hard_lim, hard_max = min(hard, key=lambda x: x[1])
    info.update(hard_max_notional=round(hard_max, 4), hard_limit=hard_lim, by_budget_notional=round(by_budget, 4))
    notional = want
    if not tm_decides:
        notional = min(want, by_budget)
        if by_budget < want - 1e-9:
            info["clipped"] = True; info["clip_by"].append("add_risk_budget")
    if notional / lev > m_room + 1e-9:
        notional = max(0.0, m_room * lev)
        info["clipped"] = True; info["clip_by"].append("max_position_pct(合并保证金)")
    if total_room is not None and notional > float(total_room) + 1e-9:
        notional = max(0.0, float(total_room))
        info["clipped"] = True; info["clip_by"].append("max_total_notional_pct")
    add_risk = notional * dist
    margin_after = float(pos.get("margin") or 0) + notional / lev
    out = {**info, "notional": notional, "add_risk": round(add_risk, 4), "budget_left": round(left - add_risk, 4), "dist": dist,
           "over_budget": bool(ex + add_risk > cap + 1e-6),
           "margin_after_pct": round(margin_after / float(equity) * 100, 3) if equity else None}
    if total_now is not None and equity:
        out["total_after_pct"] = round((float(total_now) + notional) / float(equity) * 100, 3)
    if max_total_pct is not None:
        out["max_total_pct"] = float(max_total_pct)
    if notional < max(float(min_order_usd), 1e-9):
        if tm_decides:
            if hard_max >= max(float(min_order_usd), 1e-9):
                reason = "size_too_small"
            else:
                reason = {"max_total_notional_pct": "add_total_notional_full", "max_open_risk_pct": "add_open_risk_full",
                          "max_net_directional_pct": "add_net_directional_full"}.get(hard_lim, "add_margin_full")
        else:
            reason = "add_budget_full" if (left <= 0 or "add_risk_budget" in info["clip_by"]) else (
                "add_margin_full" if info["clip_by"] else "size_too_small")
        return {**out, "ok": False, "reason": reason, "notional": 0.0, "add_risk": 0.0, "budget_left": round(left, 4)}
    return {**out, "ok": True, "reason": None}


def plan_sl_tp(rc: dict, side: str, entry: float, atr, role_stop=None, role_tp=None) -> dict:
    """加仓腿独立止盈止损：按加仓入场价，ATR+2R 基准 → 角色加权 → RR≥min_rr。与原仓止损止盈无关。"""
    from desk import sizing
    min_rr = float(rc.get("min_rr_after_adjust", 1.5))
    notes: list[str] = []
    sg = _sg(side)
    entry = float(entry)
    if atr and entry > 0:
        bs, bt, _ = sizing.stop_take(side, entry, float(atr), rc)
        fin = sizing.finalize_sl_tp(side, entry, bs, bt, role_stop, role_tp, min_rr)
        stop, tp = fin["stop"], fin["take_profit"]
        notes += fin["notes"]
    else:
        smax = float(rc.get("stop_max_pct") or 0.08)
        stop = entry * (1 - smax) if side == "long" else entry * (1 + smax)
        tp = entry + sg * min_rr * abs(entry - stop)
        notes.append(f"无 ATR，按 stop_max_pct={smax:.0%} 估止损，止盈按 RR≥{min_rr:g}")
    stop = float(stop) if stop is not None else None
    if stop is None or sg * (entry - stop) <= 0:
        return {"stop": None, "take_profit": None, "rr": None, "notes": notes + ["加仓止损无效（相对入场价方向不对）"]}
    if tp is None or sg * (float(tp) - entry) <= 0:
        tp = None
    rr = sizing.rr_ratio(side, entry, stop, tp)
    if tp is None or rr is None or rr + 1e-12 < min_rr:
        new_tp = entry + sg * min_rr * abs(entry - stop)
        notes.append(f"加仓止盈按 RR≥{min_rr:g} 外推至 {new_tp:.6g}")
        tp, rr = new_tp, min_rr
    return {"stop": stop, "take_profit": float(tp), "rr": rr, "notes": notes}


def describe(a: dict) -> str:
    """日报/notes：预算 + 原仓止盈止损与加仓腿止盈止损分开写。"""
    if not a:
        return ""
    s = (("加仓风险预算（仅参考，交易经理决定，不作否决）" if a.get("tm_decides") else "加仓风险预算")
         + f"：现有风险 {a.get('existing_risk', 0):.2f}，新增风险 {a.get('add_risk', 0):.2f}，"
         f"上限 {a.get('cap', 0):.2f}，剩余预算 {a.get('budget_left', 0):.2f} USDT，"
         + (f"已削减（{'、'.join(a.get('clip_by') or [])}）" if a.get("clipped") else "未削减"))
    if a.get("hard_max_notional") is not None:
        s += f"；硬护栏可加最大名义 {a['hard_max_notional']:.2f}（受 {a.get('hard_limit')} 约束）"
    if a.get("margin_after_pct") is not None:
        s += f"；加仓后保证金占权益 {a['margin_after_pct']:.2f}%"
    if a.get("total_after_pct") is not None:
        s += f"、总名义占权益 {a['total_after_pct']:.2f}%"
    if a.get("base_sl") is not None or a.get("base_tp") is not None:
        s += (f"；原仓止损 {a.get('base_sl')}、止盈 {a.get('base_tp')}（不动）")
    if a.get("final_sl") is not None:
        s += (f"；加仓腿入场 {a.get('add_px')}、止损 {a.get('final_sl')}、止盈 {a.get('final_tp')}、"
              f"RR {a.get('rr')}、张数 {a.get('sz')}")
    if a.get("reason"):
        s += f"，结论 {a['reason']}"
    return s


def seed_open_leg(pos: dict, ent: dict) -> list:
    """登记表尚无 legs 时，用现持仓快照写入原开仓腿，便于与加仓腿区分。"""
    legs = list(ent.get("legs") or [])
    if any(lg.get("kind") == "open" for lg in legs):
        return legs
    open_leg = {"kind": "open", "decision_id": ent.get("decision_id"), "side": pos["side"],
                "entry": float(pos.get("avg_px") or 0), "sz": str(pos.get("contracts")),
                "qty": float(pos.get("qty") or 0), "sl": pos.get("sl_px"), "tp": pos.get("tp_px"),
                "time": ent.get("opened") or pos.get("entry_time")}
    legs = [open_leg] + [lg for lg in legs if lg.get("kind") != "open"]
    ent["legs"] = legs
    return legs


def on_fill(cfg, r: dict) -> list:
    """处理 r["add_fills"]：子单/限价补单完结后复核预算（超则减加仓部分），确认本腿独立保护单，不撤旧单。"""
    from desk import demo_exec as DX, okx_demo as X, exec_lock, entry_limit
    out = []
    pend = r.get("add_fills") or {}
    for aid, m in list(pend.items()):
        inst = m["inst_id"]
        try:
            child = m.get("child_ordId")
            if child:
                d = X.get("/api/v5/trade/order", instId=inst, ordId=str(child)).get("data") or []
                if (d[0] if d else {}).get("state") in ("live", "partially_filled"):
                    out.append((aid, "waiting")); continue
            el = (entry_limit._load().get("entries") or {}).get(aid)
            if el and el.get("status") != "done":
                out.append((aid, "waiting_entry_limit")); continue
            with exec_lock.hold(inst, why="add_fill_recheck"):
                res = recheck(cfg, inst, m)
            X.log_event("add_fill_recheck", inst_id=inst, algoId=aid, **{k: v for k, v in res.items() if k != "notes"})
            pend.pop(aid, None)
            out.append((aid, res.get("result")))
        except Exception as e:
            X.log_event("add_fill_recheck_error", inst_id=inst, algoId=aid, error=f"{type(e).__name__}: {e}"[:300])
            out.append((aid, "error"))
    r["add_fills"] = pend
    return out


def recheck(cfg, inst: str, m: dict) -> dict:
    """成交后复核：超预算只减加仓部分（不低于 base_contracts）；为本腿挂独立保护单；不撤原仓保护单。"""
    from desk import demo_exec as DX, okx_demo as X, sizing
    import math
    rc = sizing.rcfg(cfg)
    bk = DX.book(cfg)
    p = next((x for x in bk["positions"] if x["inst_id"] == inst and not x.get("legacy")), None)
    if not p:
        return {"result": "no_position"}
    base = float(m.get("base_contracts") or 0)
    cur = float(p["contracts"])
    if cur <= base + 1e-12:
        return {"result": "not_filled", "contracts": cur}
    add_sz = cur - base
    side = p["side"]; sg = _sg(side)
    sl, tp = m.get("final_sl"), m.get("final_tp")
    entry = float(m.get("add_px") or p["avg_px"])
    if sl is None:
        plan = plan_sl_tp(rc, side, entry, None)
        sl, tp = plan["stop"], plan["take_profit"]
    cap = min(float(m.get("risk_cap_usd") or 0) or float("inf"), float(bk["equity"]) * float(rc["risk_per_trade_pct"]))
    ct = float(X.instrument(inst)["ctVal"])
    base_qty = float(m.get("base_qty") or 0) or base * ct
    base_sl = m.get("base_sl")
    base_avg = float(m.get("base_avg") or p["avg_px"])
    ex = max(0.0, sg * (base_avg - float(base_sl))) * base_qty if base_sl not in (None, "") else existing_risk(p, rc)
    add_risk = max(0.0, sg * (entry - float(sl))) * (add_sz * ct) if sl is not None else float("inf")
    ps = p.get("pos_side")
    old_ids = {str(a.get("algoId")) for a in DX.close_algos(inst) if a.get("posSide") == ps}
    res = {"result": "ok", "contracts": cur, "add_sz": add_sz, "sl": sl, "tp": tp,
           "existing_risk": round(ex, 4), "add_risk": round(add_risk, 4), "cap": round(cap, 4), "reduced": None,
           "old_protect_before": sorted(old_ids)}
    res["tm_decides"] = bool(m.get("tm_decides"))
    if ex + add_risk > cap + 1e-6 and sl is not None and not m.get("tm_decides"):
        per_ct = abs(entry - float(sl)) * ct
        need = (ex + add_risk - cap) / per_ct if per_ct > 0 else add_sz
        lot = float(X.instrument(inst)["lotSz"])
        cut = min(math.ceil(need / lot - 1e-9) * lot, add_sz)
        if cut > 0:
            frac = cut / cur
            DX._reduce(cfg, p, frac, "条件加仓成交后复核：超出风险预算（只减加仓部分）", restitch_protection=False)
            res.update(result="reduced", reduced=X._dec(cut))
            bk = DX.book(cfg)
            p = next((x for x in bk["positions"] if x["inst_id"] == inst and not x.get("legacy")), p)
            add_sz = max(0.0, float(p["contracts"]) - base)
            add_risk = max(0.0, sg * (entry - float(sl))) * (add_sz * ct) if sl is not None else 0.0
            res["add_risk"] = round(add_risk, 4)
            res["add_sz"] = add_sz
    # 撤掉本腿旧保护（同 from_algo / 超覆盖的附带单），再按剩余加仓张数重挂；原仓 algo 不动
    known_add = str(m.get("protect_algoId") or "")
    if known_add and known_add in old_ids:
        try:
            X.post("/api/v5/trade/cancel-algos", [{"instId": inst, "algoId": known_add}])
        except Exception:
            pass
    pid = None
    if add_sz > 0 and sl is not None:
        # 若附带止损已存在且张数≈add_sz，复用；否则新挂本腿
        fresh = [a for a in DX.close_algos(inst) if a.get("posSide") == ps and str(a.get("algoId")) not in old_ids]
        match = [a for a in fresh if abs(float(a.get("sz") or 0) - add_sz) < 1e-9]
        if match:
            pid = match[0].get("algoId")
        else:
            pid = DX.place_leg_protection(p, sl, tp, add_sz)
        r = DX.reg()
        ent = r["positions"].setdefault(inst, {"decision_id": None, "side": side})
        seed_open_leg({**p, "contracts": base, "qty": base_qty, "avg_px": base_avg, "sl_px": base_sl, "tp_px": m.get("base_tp")}, ent)
        # 原开仓腿若刚 seed，用成交前快照
        for lg in ent["legs"]:
            if lg.get("kind") == "open":
                lg.update(entry=base_avg, sz=X._dec(base), qty=base_qty, sl=base_sl, tp=m.get("base_tp"))
                break
        ent["legs"] = [lg for lg in ent["legs"] if str(lg.get("from_algo")) != str(m.get("algoId") or "")]
        ent["legs"].append({"kind": "add", "decision_id": m.get("decision_id"), "side": side, "entry": entry,
                            "sz": X._dec(add_sz), "qty": add_sz * ct, "sl": sl, "tp": tp,
                            "protect_algoId": pid, "from_algo": m.get("algoId"), "time": settings_now()})
        DX.save_reg(r)
    still = {str(a.get("algoId")) for a in DX.close_algos(inst) if a.get("posSide") == ps}
    res["old_protect_kept"] = sorted(old_ids & still)
    res["protect_algoId"] = pid
    if add_sz > 0 and not pid:
        res["result"] = res["result"] + "+protect_failed"
    if old_ids - still - ({known_add} if known_add else set()):
        res["result"] = res["result"] + "+old_protect_lost"
    return res


def settings_now() -> str:
    from desk import settings
    return settings.now_str()
