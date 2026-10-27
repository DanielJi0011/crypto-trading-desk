"""OKX 模拟盘直连执行层（组合经理执行、护栏数据、日报数据都从这里取）。

!! 禁止实盘：所有网络调用都经 desk/okx_demo.py 的 _request —— 只用 OKX_DEMO_* 密钥、强制 x-simulated-trading: 1、
!! 启动自检失败即拒绝下单，不回退实盘 key。本模块不写本地纸面账本（trades.csv/account.json），也不写本地 pending。
!! 两个旧纸面仓（BTC/ETH）只由 desk.monitor（旧仓盯盘）管理，本模块对它们不做任何模拟盘动作，只在护栏里计数。

本地只保存元数据 state/ledger/demo_orders.json：
  triggers[algoId] = {inst_id, decision_id, side, expires, horizon_days, ...}   条件单的有效期/来源
  positions[inst]  = {decision_id, deadline, ...}                             持有期到期时间
  watch            = {last_algo_uts}                                         止损检测游标
"""
from __future__ import annotations

import datetime as _dt
import math
import uuid

from desk import okx_demo as X
from desk import settings

REG = settings.STATE / "ledger" / "demo_orders.json"
SIGN = {"long": 1, "short": -1}


def ecfg(cfg) -> dict:
    d = {"venue": "okx_demo", "equity_basis": "initial", "cancel_pending_on_new_decision": True,
         "protected_algo_ids": [], "trigger_px_type": "last", "stop_coverage_auto_replace": False}
    out = {**d, **(cfg.get("execution") or {})}
    out["entry_limit"] = {**ENTRY_LIMIT_DEFAULTS, **(out.get("entry_limit") or {})}
    return out


ENTRY_LIMIT_DEFAULTS = {"enabled": True, "offset_pct": 0.1, "timeout_minutes": 3, "fallback_market": True, "ws_check_secs": 20}


def entry_limit_px(side: str, trigger_px: float, offset_pct: float, tick: float) -> float:
    """条件单触发后的限价：做多=触发价×(1-偏移%)，做空=触发价×(1+偏移%)，按 tick 四舍五入取整。"""
    f = 1 - offset_pct / 100 if side == "long" else 1 + offset_pct / 100
    n = round(trigger_px * f / tick)
    return float(f"{n * tick:.12g}")


DEMO_ACCOUNT = settings.STATE / "demo_account.json"


def initial_equity() -> float:
    v = settings.read_json(DEMO_ACCOUNT, {}).get("initial_equity_usdt")
    if not v:
        raise X.DemoError("state/demo_account.json 缺少 initial_equity_usdt（equity_basis=initial 时必需）")
    return float(v)


def basis_equity(cfg, live: float) -> float:
    """仓位%与护栏的权益基准：execution.equity_basis = initial（固定初始权益，默认）| live（模拟盘实时 USDT 权益）。"""
    b = ecfg(cfg)["equity_basis"]
    if b == "initial":
        return initial_equity()
    if b == "live":
        return live
    raise ValueError(f"execution.equity_basis 只能是 initial 或 live，当前 {b}")


def is_demo(cfg) -> bool:
    if settings.ROOT.resolve() != settings.CODE.resolve():   # 测试/临时数据目录：一律走纸面，绝不访问模拟盘
        return False
    return ecfg(cfg)["venue"] == "okx_demo"


def reg() -> dict:
    r = settings.read_json(REG, {})
    r.setdefault("triggers", {}); r.setdefault("positions", {}); r.setdefault("watch", {})
    return r


def save_reg(r):
    settings.write_json(REG, r)


def _ts(ms) -> str:
    return _dt.datetime.fromtimestamp(int(ms) / 1000, settings.TZ).strftime("%Y-%m-%d %H:%M:%S%z")


def _require():
    if not X.verify():
        raise X.DemoError("模拟环境自检失败，拒绝下单（禁止回退实盘）")


# ------------------------------------------------------------------ 读取：权益 / 持仓 / 挂单
def usdt_equity() -> float:
    bal = X.get("/api/v5/account/balance", ccy="USDT")["data"][0]
    d = next((x for x in bal.get("details", []) if x.get("ccy") == "USDT"), {})
    return float(d.get("eq") or 0)


def demo_positions() -> list[dict]:
    return [p for p in X.get("/api/v5/account/positions", instType="SWAP")["data"] if float(p.get("pos") or 0)]


def close_algos(inst=None) -> list[dict]:
    out = []
    for t in ("oco", "conditional"):
        kw = {"ordType": t, **({"instId": inst} if inst else {"instType": "SWAP"})}
        out += X.get("/api/v5/trade/orders-algo-pending", **kw).get("data", [])
    return out


def trigger_orders(inst=None) -> list[dict]:
    kw = {"ordType": "trigger", **({"instId": inst} if inst else {"instType": "SWAP"})}
    return X.get("/api/v5/trade/orders-algo-pending", **kw).get("data", [])


def pending_entry_orders(inst=None) -> list[dict]:
    """R12（第一批）：/trade/orders-pending 中开仓方向、非 reduceOnly 的普通挂单（条件单触发后的限价子单、补单等在途入场）。"""
    from desk.trigger_review import is_entry
    kw = {"instId": inst} if inst else {"instType": "SWAP"}
    return [o for o in X.get("/api/v5/trade/orders-pending", **kw).get("data", []) if is_entry(o)]


def pending_entry_risk(cfg, orders) -> list[dict]:
    """在途入场子单的剩余名义与止损风险（无附带止损价时按 guardrails.max_stop_loss_pct 计）。"""
    out = []
    for o in orders:
        ct = float(X.instrument(o["instId"])["ctVal"])
        rem = max(float(o.get("sz") or 0) - float(o.get("accFillSz") or 0), 0.0)
        px = float(o.get("px") or 0) or float(o.get("avgPx") or 0) or float(o.get("last") or 0)
        notional = rem * ct * px
        sl = None
        for a in o.get("attachAlgoOrds") or []:
            if a.get("slTriggerPx"):
                sl = float(a["slTriggerPx"])
        if sl is None and o.get("slTriggerPx"):
            sl = float(o["slTriggerPx"])
        dist = abs(px - sl) / px if (sl and px) else float(cfg["guardrails"].get("max_stop_loss_pct", 15)) / 100
        out.append({"inst_id": o["instId"], "ordId": o.get("ordId"), "notional": notional, "stop_risk": notional * dist,
                    "side": "long" if o.get("side") == "buy" else "short"})
    return out


def trigger_risk(cfg, triggers) -> list[dict]:
    """条件入场单按触发价的名义与止损风险（R4 组合层）。无附带止损按 risk.stop_max_pct。"""
    from desk import sizing
    smax = float(sizing.rcfg(cfg)["stop_max_pct"])
    out = []
    for t in triggers:
        px = float(t["triggerPx"])
        n = float(t["sz"]) * float(X.instrument(t["instId"])["ctVal"]) * px
        sl = next((float(a["slTriggerPx"]) for a in t.get("attachAlgoOrds") or [] if a.get("slTriggerPx")), None)
        d = abs(px - sl) / px if sl else smax
        out.append({"inst_id": t["instId"], "side": "long" if t.get("side") == "buy" else "short", "notional": n, "stop_risk": n * d})
    return out


def book(cfg, prices: dict | None = None) -> dict:
    """与 ledger.book 同结构。equity=护栏/仓位基准（execution.equity_basis：initial=state/demo_account.json 初始权益，live=实时）；live_equity=模拟盘实时 USDT 权益。
    positions 含模拟盘持仓 + 旧纸面仓（legacy=True，只计数、不操作）。"""
    from desk import ledger
    _require()
    tick = {c["inst_id"]: c["ticker"] for c in cfg["coins"]}
    r = reg()
    algos = close_algos()
    pos = []
    for p in demo_positions():
        inst = p["instId"]; ct = float(X.instrument(inst)["ctVal"])
        sd = p["posSide"] if p["posSide"] in ("long", "short") else ("long" if float(p["pos"]) > 0 else "short")
        qty = abs(float(p["pos"])) * ct
        px = float((prices or {}).get(inst) or p.get("last") or p.get("markPx") or p["avgPx"])
        mine = [a for a in algos if a["instId"] == inst and a.get("posSide") in (p["posSide"], "net")]
        sl = next((float(a["slTriggerPx"]) for a in mine if a.get("slTriggerPx")), None)
        tp = next((float(a["tpTriggerPx"]) for a in mine if a.get("tpTriggerPx")), None)
        margin = float(p.get("margin") or p.get("imr") or 0)
        upl = float(p.get("upl") or 0)
        pos.append({"trade_id": f"DEMO-{inst}-{p['posSide']}", "inst_id": inst, "ticker": tick.get(inst, inst), "side": sd,
                    "leverage": float(p.get("lever") or 1), "margin": margin, "notional": qty * px,
                    "liq_px": float(p["liqPx"]) if p.get("liqPx") else None, "funding": 0.0, "qty": qty,
                    "contracts": abs(float(p["pos"])), "pos_side": p["posSide"], "avg_px": float(p["avgPx"]), "price": px,
                    "value": margin + upl, "sl_px": sl, "tp_px": tp, "upnl": upl, "entry_time": _ts(p["cTime"]),
                    "deadline": r["positions"].get(inst, {}).get("deadline"), "venue": "okx_demo"})
    paper = ledger.book(cfg, prices)
    for p in paper["positions"]:
        pos.append({**p, "legacy": True, "venue": "paper(旧仓)"})
    live = usdt_equity()
    eq = basis_equity(cfg, live)
    demo_pos = [p for p in pos if not p.get("legacy")]
    ini = initial_equity() if DEMO_ACCOUNT.exists() else live
    return {"cash": live - sum(p["value"] for p in demo_pos), "equity": eq, "live_equity": live, "initial": ini,
            "positions": pos, "exposure": sum(p["notional"] for p in demo_pos), "margin": sum(p["margin"] for p in demo_pos),
            "triggers": trigger_orders(), "equity_basis": ecfg(cfg)["equity_basis"], "paper_equity": paper["equity"],
            # R12：在途入场子单（读取失败则 book 失败，护栏拒绝开仓，不按“无挂单”处理）
            "pending_entries": pending_entry_risk(cfg, pending_entry_orders())}


# ------------------------------------------------------------------ 护栏（与 ledger.check_open 同规则，数据来自模拟盘）
def today_realized() -> float:
    start = settings.now().replace(hour=0, minute=0, second=0, microsecond=0)
    ms = int(start.timestamp() * 1000)
    return sum(float(x.get("realizedPnl") or 0) for x in positions_history(since_ms=ms) if int(x.get("uTime") or 0) >= ms)


def positions_history(since_ms: int = 0, max_pages: int = 50) -> list[dict]:
    """R5：positions-history 翻页取全（每页 100 条，按 uTime 倒序；after=上一页最后一条的 uTime）。
    取到早于 since_ms 的记录或空页即停；超过 max_pages 仍未取完 → 抛错（调用方按读取失败拒绝）。"""
    out, after = [], None
    for _ in range(max_pages):
        kw = {"instType": "SWAP", "limit": 100, **({"after": after} if after else {})}
        rows = X.get("/api/v5/account/positions-history", **kw).get("data", [])
        out.extend(rows)
        if len(rows) < 100 or not rows:
            return out
        last = int(rows[-1].get("uTime") or 0)
        if last < since_ms:
            return out
        after = str(last)
    raise X.DemoError(f"positions-history 超过 {max_pages} 页仍未取完")


def today_orders() -> int:
    d = settings.today()
    # O3：只统计 strategy 与 rediscuss；旧事件无 source 字段时按计入处理（保守）
    return sum(1 for e in settings.read_jsonl(X.EVENTS)
               if e.get("event") in ("open_ok", "trigger_placed") and str(e.get("time", "")).startswith(d)
               and e.get("source", "strategy") in ("strategy", "rediscuss"))


def risk_pct(cfg) -> float:
    return float((cfg.get("risk") or {}).get("risk_per_trade_pct", 0.0075))


def risk_cap_usd(cfg, equity: float) -> float:
    return float(equity) * risk_pct(cfg)


def clip_to_risk(cfg, bk, inst, sz, px, sl, decision_id=None):
    """按单笔止损亏损上限削减张数（只减不增）。返回新张数（字符串或 None）。削减时记事件 size_clipped。"""
    if not sz or not sl:
        return sz
    ct = float(X.instrument(inst)["ctVal"])
    qty = float(sz) * ct
    cap = risk_cap_usd(cfg, bk["equity"])
    loss = qty * abs(float(px) - float(sl))
    if loss <= cap + 1e-9:
        return sz
    new = X.contracts(inst, cap / abs(float(px) - float(sl)))
    X.log_event("size_clipped", inst_id=inst, decision_id=decision_id, sz_before=sz, sz_after=new, limit="risk_per_trade_pct",
                stop_loss_usd_before=round(loss, 4), cap_usd=round(cap, 4))
    return new


def check_open(cfg, bk, inst, qty, px, sl, side, lev, add: dict | None = None):
    """开仓护栏。add=本币现有持仓（同向加仓模式，risk.allow_add_position）：不查单币一仓、同币加仓不算新仓；
    保证金按合并后整仓；强平按加仓后均价与原杠杆；单笔风险 = 现有风险（现止损到均价，盈利区按 0）+ 加仓风险。其余护栏不变。"""
    from desk import ledger
    g, sw = cfg["guardrails"], ledger.swap_cfg(cfg)
    reasons, checks = [], {}
    fail = reasons.append
    eq = bk["equity"]
    if settings.kill_active():
        fail("[KILL] 紧急停止文件存在，禁止开仓")
    if settings.halt_active():
        fail("[HALT] 全冻结文件存在，禁止开仓")
    if inst not in [c["inst_id"] for c in cfg["coins"]]:
        fail(f"[UNIVERSE] {inst} 不在品种池")
    from desk import config_safety
    lev_cap = config_safety.max_open_leverage(cfg)   # Q11：新开仓按更严的 1 倍（guardrails.leverage / allow_leverage 与 swap.leverage_max 取最严）
    if not (1 <= lev <= lev_cap):
        fail(f"[LEVERAGE] 杠杆 {lev} 超出新开仓上限 1~{lev_cap:g}")
    notional = qty * px; margin = notional / max(lev, 1e-9)
    checks.update(notional_usd=round(notional, 4), margin_usd=round(margin, 4), leverage=lev, equity=round(eq, 4))
    if add is not None:
        if add.get("side") != side:
            fail("[ADD] 加仓方向与持仓相反（不反手，须先平仓）")
        margin = margin + float(add.get("margin") or 0)   # 单笔保证金上限按合并后整仓
        checks["margin_after_add_usd"] = round(margin, 4)
    if qty <= 0 or px <= 0:
        fail("[SIZE] 数量与价格必须为正")
    if margin > eq * g["max_position_pct"] / 100 + 1e-9:
        fail(f"[POSITION] 保证金 {margin:.2f} 超过单笔上限 {eq * g['max_position_pct'] / 100:.2f}（{g['max_position_pct']}%）")
    if notional < g.get("min_order_usd", 0):
        fail(f"[MIN_ORDER] 名义价值低于 {g.get('min_order_usd')} USD")
    if "pending_entries" not in bk:   # R12：护栏视图必须包含在途入场子单；缺失即拒绝（fail-closed）
        fail("[GUARD_DATA] 护栏视图缺少在途入场子单（orders-pending），拒绝开仓")
    pend = bk.get("pending_entries") or []
    pend_insts = {p["inst_id"] for p in pend}
    trig_insts = {t["instId"] for t in bk["triggers"]}
    held_insts = {p["inst_id"] for p in bk["positions"]}
    n_open = len(bk["positions"]) + len((trig_insts | pend_insts) - held_insts - {inst})
    if add is not None and inst in held_insts:
        n_open -= 1   # 同币加仓不算新仓
    if inst in pend_insts:
        fail("[ONE_PER_COIN] 该币有未完结的入场子单（视同已有持仓），不再开仓")
    checks["pending_entry_notional"] = round(sum(p["notional"] for p in pend), 4)
    checks["pending_entry_stop_risk"] = round(sum(p["stop_risk"] for p in pend), 4)
    if n_open >= g.get("max_open_positions", 3):
        fail(f"[MAX_POS] 持仓+其他币挂单 {n_open} 已达上限")
    if add is None and any(p["inst_id"] == inst for p in bk["positions"]):
        fail("[ONE_PER_COIN] 该币已有持仓（含旧纸面仓），不加仓")
    if add is not None and any(p["inst_id"] == inst and p.get("legacy") for p in bk["positions"]):
        fail("[ADD] 旧纸面仓不加仓")
    trig_notional = sum(float(t["sz"]) * float(X.instrument(t["instId"])["ctVal"]) * float(t["triggerPx"])
                        for t in bk["triggers"] if t["instId"] != inst)
    exp = bk["exposure"] + trig_notional + sum(p["notional"] for p in pend)   # R12：在途入场子单名义计入总敞口
    max_tot = g.get("max_total_notional_pct") or g.get("max_total_exposure_pct", 60) * float(sw["leverage_max"])
    if exp + notional > eq * max_tot / 100 + 1e-9:
        fail(f"[EXPOSURE] 总名义（含挂单）将达 {exp + notional:.2f}，超过 {eq * max_tot / 100:.2f}（{max_tot}%）")
    # R4：同向净名义与在途风险（持仓 + 其他币条件单 + 在途入场子单）
    from desk import sizing
    rc = sizing.rcfg(cfg)
    try:
        ex = sizing.book_exposure({**bk, "trigger_risk": trigger_risk(cfg, [t for t in bk["triggers"] if t["instId"] != inst])}, rc)
        sg = 1 if side == "long" else -1
        net_after = sg * ex["net"] + notional
        dist_new = abs(px - sl) / px if (sl and px) else float(rc["stop_max_pct"])
        risk_after = ex["open_risk"] + notional * dist_new
        checks.update(net_directional_after=round(net_after, 2), open_risk_after=round(risk_after, 2))
        if net_after > eq * float(rc["max_net_directional_pct"]) + 1e-6:
            fail(f"[NET_DIRECTIONAL] 同向净名义将达 {net_after:.2f}，超过 {eq * float(rc['max_net_directional_pct']):.2f}")
        if risk_after > eq * float(rc["max_open_risk_pct"]) + 1e-6:
            fail(f"[OPEN_RISK] 在途止损风险将达 {risk_after:.2f}，超过 {eq * float(rc['max_open_risk_pct']):.2f}")
    except Exception as e:
        fail(f"[GUARD_DATA] 组合层敞口计算失败（{type(e).__name__}），拒绝开仓")
    liq_px_base = px
    if add is not None:   # 强平按加仓后均价（原杠杆）
        from desk import add_position as AP
        liq_px_base = AP.new_avg(add, qty, px)
        checks["avg_after_add"] = round(liq_px_base, 6)
    liq = ledger.liq_price(side, liq_px_base, max(lev, 1e-9), float(sw["maint_margin_rate"]))
    checks["liq_px"] = liq
    if sl is None:
        fail("[STOP_LOSS] 开仓必须附带止损")
    else:
        if side == "long" and sl >= px:
            fail(f"[STOP_LOSS] 多单止损 {sl} 必须低于入场 {px:.6g}")
        if side == "short" and sl <= px:
            fail(f"[STOP_LOSS] 空单止损 {sl} 必须高于入场 {px:.6g}")
        dist = abs(px - sl) / px * 100
        checks["sl_distance_pct"] = round(dist, 3)
        if dist > g.get("max_stop_loss_pct", 100):
            fail(f"[STOP_LOSS] 止损距离 {dist:.2f}% 超过上限 {g.get('max_stop_loss_pct')}%")
        if (side == "long" and sl <= liq) or (side == "short" and sl >= liq):
            fail(f"[LIQUIDATION] 止损 {sl:.6g} 不会先于强平价 {liq:.6g} 触发")
        cap = risk_cap_usd(cfg, eq)   # 单笔止损亏损上限（risk.risk_per_trade_pct，R1 的上限部分）
        loss = qty * abs(px - sl)
        if add is not None:   # 现有风险 + 加仓风险 ≤ 单笔上限
            from desk import add_position as AP, sizing as _sz
            ex_r = AP.existing_risk(add, _sz.rcfg(cfg))
            checks.update(existing_risk_usd=round(ex_r, 4), add_risk_usd=round(loss, 4))
            loss += ex_r
        checks.update(stop_loss_usd=round(loss, 4), risk_cap_usd=round(cap, 4))
        if loss > cap + 1e-6:
            fail(f"[RISK_PER_TRADE] 止损亏损 {loss:.2f} 超过单笔上限 {cap:.2f}（{float(risk_pct(cfg)):.2%}×权益）")
    # R5（第一批）：出错即拒绝。当日亏损按实时总权益（含浮亏）对比 day_start_equity，回撤对比 peak_equity（desk/equity_guard.py）
    from desk import equity_guard
    reasons.extend(equity_guard.reasons(cfg, bk.get("live_equity")))
    try:   # 已实现亏损（旧口径，首页）保留为附加检查；读取失败同样拒绝，不再按 0 处理
        pnl = today_realized()
        den = initial_equity() if DEMO_ACCOUNT.exists() else eq
        if pnl <= -den * g["max_daily_loss_pct"] / 100:
            fail(f"[DAILY_LOSS] 模拟盘当日已实现亏损 {pnl:.2f} 达到上限，今日禁止开仓")
    except Exception as e:
        X.log_event("guard_data_unavailable", what="positions-history", error=f"{type(e).__name__}"[:200])
        fail(f"[GUARD_DATA] 当日已实现盈亏读取失败（{type(e).__name__}），拒绝开仓")
    n, lim = today_orders(), int(g.get("max_orders_per_day") or 0)
    if lim > 0 and n >= lim:   # 0 = 不限（2026-10-23 用户取消每日单数上限）
        fail(f"[MAX_ORDERS] 今日开仓/挂单 {n} 已达上限")
    return reasons, checks


# ------------------------------------------------------------------ 执行
def _ps(side):
    return side if X.pos_mode() == "long_short_mode" else "net"


def _set_lev(inst, lev, ps):
    body = {"instId": inst, "lever": X._dec(float(lev)), "mgnMode": "isolated"}
    if ps != "net":
        body["posSide"] = ps
    X.post("/api/v5/account/set-leverage", body)


def _attach(inst, sl, tp, key=None):
    a = {"attachAlgoClOrdId": det_id("att", key) if key else "ta" + uuid.uuid4().hex[:20]}
    if sl:
        a.update(slTriggerPx=X._px(inst, sl), slOrdPx="-1", slTriggerPxType="last")
    if tp:
        a.update(tpTriggerPx=X._px(inst, tp), tpOrdPx="-1", tpTriggerPxType="last")
    return a


def cancel_triggers(cfg, inst, why) -> list:
    """撤该币的条件入场单（受 protected_algo_ids 保护的不撤）。"""
    prot = {str(x) for x in ecfg(cfg)["protected_algo_ids"]}
    done = []
    for t in trigger_orders(inst):
        if t["algoId"] in prot:
            X.log_event("trigger_kept_protected", inst_id=inst, algoId=t["algoId"], why=why); continue
        X.post("/api/v5/trade/cancel-algos", [{"instId": inst, "algoId": t["algoId"]}])
        r = reg(); r["triggers"].pop(t["algoId"], None); save_reg(r)
        X.log_event("trigger_cancelled", inst_id=inst, algoId=t["algoId"], why=why); done.append(t["algoId"])
    return done


def open_now(cfg, bk, inst, size_pct, sl, tp, side, lev, decision_id, horizon_days, ref_px):
    _require()
    notional = size_pct * bk["equity"] / 100 * lev
    sz = X.contracts(inst, notional / ref_px)
    sz = clip_to_risk(cfg, bk, inst, sz, ref_px, sl, decision_id)
    qty = float(sz or 0) * float(X.instrument(inst)["ctVal"])
    reasons, checks = check_open(cfg, bk, inst, qty, ref_px, sl, side, lev)
    if not sz:
        reasons.append("[SIZE] 不足最小张数")
    if trigger_orders(inst):
        reasons.append("[ONE_PER_COIN] 该币仍有（受保护的）条件入场单，不再现价开仓")
    if pending_entry_orders(inst):   # R12：下单前再查一次同币在途入场子单
        reasons.append("[ONE_PER_COIN] 该币有未完结的入场子单，不再现价开仓")
    if reasons:
        X.log_event("open_rejected", inst_id=inst, decision_id=decision_id, reasons=reasons)
        return False, {"reasons": reasons, "checks": checks}
    ps = _ps(side); _set_lev(inst, lev, ps)
    cl = det_id("open", inst, decision_id or "-", side, sz, settings.now_str()[:16])   # R11.1：同一决定同一分钟重试同号，交易所拒重
    body = {"instId": inst, "tdMode": "isolated", "side": "buy" if side == "long" else "sell", "ordType": "market", "sz": sz,
            "clOrdId": cl, "attachAlgoOrds": [_attach(inst, sl, tp, cl)]}
    if ps != "net":
        body["posSide"] = ps
    d = X.post("/api/v5/trade/order", body)["data"][0]
    r = reg(); r["positions"][inst] = {"decision_id": decision_id, "side": side, "opened": settings.now_str(), "child_ordId": d.get("ordId"),
                                       "deadline": (settings.now() + _dt.timedelta(days=horizon_days)).strftime("%Y-%m-%d %H:%M:%S%z")}
    save_reg(r)
    from desk import entry_state
    entry_state.advance(d.get("ordId"), "open_sent", inst_id=inst, decision_id=decision_id)
    X.log_event("open_ok", inst_id=inst, decision_id=decision_id, ordId=d.get("ordId"), sz=sz, side=side, lever=lev, sl=sl, tp=tp)
    return True, {"trade_id": f"DEMO-{inst}-{ps}", "ord_id": d.get("ordId"), "sz": sz, "qty": qty, "px": ref_px, "side": side,
                  "leverage": lev, "margin": qty * ref_px / lev, "notional": qty * ref_px, "liq_px": checks.get("liq_px"), "checks": checks}


def place_trigger(cfg, bk, inst, o: dict):
    """条件入场单：OKX trigger 算法单（市价触发）+ attachAlgoOrds 止损止盈。o 同旧 pending 字段。"""
    _require()
    side, lev, px = o["side"], float(o.get("leverage") or 1), float(o["trigger_px"])
    sz = X.contracts(inst, float(o["size_pct"]) * bk["equity"] / 100 * lev / px)
    sz = clip_to_risk(cfg, bk, inst, sz, px, o.get("stop_loss"), o.get("decision_id"))
    qty = float(sz or 0) * float(X.instrument(inst)["ctVal"])
    bk2 = {**bk, "triggers": [t for t in bk["triggers"] if t["instId"] != inst]}   # 同币在途入场子单不剔除：存在即拒绝（R12）
    reasons, checks = check_open(cfg, bk2, inst, qty, px, o.get("stop_loss"), side, lev)
    if not sz:
        reasons.append("[SIZE] 不足最小张数")
    if reasons:
        X.log_event("trigger_rejected", inst_id=inst, decision_id=o.get("decision_id"), reasons=reasons)
        return False, {"reasons": reasons, "checks": checks}
    cancel_triggers(cfg, inst, "新条件单替换")
    if trigger_orders(inst):
        return False, {"reasons": ["该币已有受保护的条件单，不替换"], "checks": checks}
    expires = (settings.now() + _dt.timedelta(hours=float(o.get("expire_hours") or 24))).strftime("%Y-%m-%d %H:%M:%S%z")
    meta = submit_trigger(cfg, inst, o, sz, expires)
    return True, {**meta, "id": meta["algoId"]}


def place_leg_protection(p, sl, tp, contracts) -> str | None:
    """为加仓腿挂一张只覆盖本腿张数的 reduceOnly 止盈止损；不撤任何既有保护单。返回新 algoId。"""
    inst = p["inst_id"]
    sz = X._dec(contracts)
    body = {"instId": inst, "tdMode": "isolated", "side": "sell" if p["side"] == "long" else "buy",
            "ordType": "oco" if (sl and tp) else "conditional", "sz": sz, "reduceOnly": True,
            "algoClOrdId": det_id("addp", inst, p.get("pos_side", "net"), sl, tp, sz, settings.now_str()[:16])}
    if p.get("pos_side", "net") != "net":
        body["posSide"] = p["pos_side"]
    if sl:
        body.update(slTriggerPx=X._px(inst, sl), slOrdPx="-1", slTriggerPxType="last")
    if tp:
        body.update(tpTriggerPx=X._px(inst, tp), tpOrdPx="-1", tpTriggerPxType="last")
    try:
        new_id = X.post("/api/v5/trade/order-algo", body)["data"][0].get("algoId")
    except Exception as e:
        _alert("add_leg_protect_failed", inst_id=inst, step="place", error=f"{type(e).__name__}: {e}"[:300], sz=sz, sl=sl, tp=tp)
        return None
    if not _algo_live(inst, new_id):
        _alert("add_leg_protect_failed", inst_id=inst, step="confirm", algoId=new_id, kept="all_old")
        return None
    X.log_event("add_leg_protect_ok", inst_id=inst, algoId=new_id, sz=sz, sl=sl, tp=tp)
    return new_id


def add_now(cfg, bk, inst, pos, sz, sl, tp, decision_id, ref_px, add_info=None):
    """同向现价加仓：过加仓模式护栏 → 市价单并附带本腿止盈止损（只覆盖加仓张数）→
    登记加仓腿。不改杠杆、不撤原仓保护单、不 consolidate。"""
    from desk import add_position as AP
    _require()
    side, lev = pos["side"], float(pos.get("leverage") or 1)
    qty = float(sz or 0) * float(X.instrument(inst)["ctVal"])
    reasons, checks = check_open(cfg, bk, inst, qty, ref_px, sl, side, lev, add=pos)
    if not sz:
        reasons.append("[SIZE] 不足最小张数")
    if trigger_orders(inst):
        reasons.append("[ONE_PER_COIN] 该币仍有（受保护的）条件入场单，不再现价加仓")
    if pending_entry_orders(inst):
        reasons.append("[ONE_PER_COIN] 该币有未完结的入场子单，不再现价加仓")
    if reasons:
        X.log_event("add_rejected", inst_id=inst, decision_id=decision_id, reasons=reasons)
        return False, {"reasons": reasons, "checks": checks}
    ps = pos.get("pos_side") or _ps(side)
    # 下单前记下原仓保护单，事后核对未被撤
    old_ids = {str(a.get("algoId")) for a in close_algos(inst) if a.get("posSide") == ps}
    cl = det_id("add", inst, decision_id or "-", side, sz, settings.now_str()[:16])
    body = {"instId": inst, "tdMode": "isolated", "side": "buy" if side == "long" else "sell", "ordType": "market", "sz": sz,
            "clOrdId": cl, "attachAlgoOrds": [_attach(inst, sl, tp, cl)]}
    if ps != "net":
        body["posSide"] = ps
    d = X.post("/api/v5/trade/order", body)["data"][0]
    # 附带止损止盈由交易所按本单张数生成；若未出现则补挂本腿保护单（仍不撤旧）
    pid = None
    try:
        import time as _t
        for _ in range(6):
            now = [a for a in close_algos(inst) if a.get("posSide") == ps and str(a.get("algoId")) not in old_ids]
            if now:
                pid = now[0].get("algoId"); break
            _t.sleep(0.3)
    except Exception:
        pass
    if not pid:
        pid = place_leg_protection(pos, sl, tp, float(sz))
    still = {str(a.get("algoId")) for a in close_algos(inst) if a.get("posSide") == ps}
    if old_ids - still:
        X.log_event("add_old_protect_missing", inst_id=inst, missing=list(old_ids - still))
    r = reg()
    ent = r["positions"].setdefault(inst, {"decision_id": None, "side": side, "opened": pos.get("entry_time")})
    AP.seed_open_leg(pos, ent)
    leg = {"kind": "add", "decision_id": decision_id, "side": side, "entry": float(ref_px), "sz": sz, "qty": qty,
           "sl": sl, "tp": tp, "ordId": d.get("ordId"), "protect_algoId": pid, "time": settings.now_str()}
    if add_info:
        for k in ("existing_risk", "add_risk", "budget_left", "clipped", "rr"):
            if k in add_info:
                leg[k] = add_info[k]
    ent.setdefault("legs", []).append(leg)
    ent.setdefault("adds", []).append({k: leg[k] for k in ("decision_id", "ordId", "sz", "sl", "tp", "entry", "time", "protect_algoId") if k in leg})
    save_reg(r)
    X.log_event("add_ok", inst_id=inst, decision_id=decision_id, ordId=d.get("ordId"), sz=sz, side=side, lever=lev, sl=sl, tp=tp,
                protect_algoId=pid, old_protect_kept=list(old_ids),
                **({k: add_info.get(k) for k in ("existing_risk", "add_risk", "budget_left", "clipped")} if add_info else {}))
    return True, {"trade_id": f"DEMO-{inst}-{ps}", "ord_id": d.get("ordId"), "sz": sz, "qty": qty, "px": ref_px, "side": side,
                  "leverage": lev, "margin": qty * ref_px / lev, "notional": qty * ref_px, "liq_px": checks.get("liq_px"),
                  "checks": checks, "add": True, "protect_algoId": pid, "sl": sl, "tp": tp,
                  "old_protect_kept": sorted(old_ids), "leg": leg}


def place_add_trigger(cfg, bk, inst, pos, o: dict, sz: str, add_meta: dict):
    """条件加仓单：加仓模式护栏；附带本腿独立止盈止损。成交后 on_fill 复核预算并确认本腿保护，不撤原仓保护单。"""
    _require()
    side, px = pos["side"], float(o["trigger_px"])
    lev = float(pos.get("leverage") or 1)
    qty = float(sz or 0) * float(X.instrument(inst)["ctVal"])
    bk2 = {**bk, "triggers": [t for t in bk["triggers"] if t["instId"] != inst]}
    reasons, checks = check_open(cfg, bk2, inst, qty, px, o.get("stop_loss"), side, lev, add=pos)
    if not sz:
        reasons.append("[SIZE] 不足最小张数")
    if reasons:
        X.log_event("add_trigger_rejected", inst_id=inst, decision_id=o.get("decision_id"), reasons=reasons)
        return False, {"reasons": reasons, "checks": checks}
    cancel_triggers(cfg, inst, "新条件加仓单替换")
    if trigger_orders(inst):
        return False, {"reasons": ["该币已有受保护的条件单，不替换"], "checks": checks}
    expires = (settings.now() + _dt.timedelta(hours=float(o.get("expire_hours") or 24))).strftime("%Y-%m-%d %H:%M:%S%z")
    meta = submit_trigger(cfg, inst, {**o, "leverage": lev}, sz, expires, extra={"add": dict(add_meta)}, keep_lev=True)
    return True, {**meta, "id": meta["algoId"]}


TRIGGER_META_KEYS = ("decision_id", "source", "side", "leverage", "trigger_px", "condition", "stop_loss", "take_profit",
                     "size_pct", "horizon_days", "consensus", "reason")


def submit_trigger(cfg, inst, o: dict, sz: str, expires: str, force_market: bool = False, extra: dict | None = None,
                   keep_lev: bool = False) -> dict:
    """按给定张数挂一张条件入场单（不重新算仓位、不过护栏——调用方负责先过 check_open）。
    entry_limit.enabled 且非 force_market 时 orderPx=新逻辑限价并登记超时跟踪，否则 orderPx=-1（市价触发）。
    写入登记表 triggers[algoId] 并返回 meta。place_trigger 与 scripts/reprice_triggers_limit.py 共用。"""
    _require()
    side, lev, px = o["side"], float(o.get("leverage") or 1), float(o["trigger_px"])
    ps = _ps(side)
    if not keep_lev:   # 加仓单：逐仓 set-leverage 作用于整仓，沿用现有持仓杠杆，不改
        _set_lev(inst, lev, ps)
    el = ecfg(cfg)["entry_limit"]
    order_px = "-1"
    if el.get("enabled") and not force_market:
        order_px = X._dec(entry_limit_px(side, px, float(el["offset_pct"]), float(X.instrument(inst)["tickSz"])))
    cl = det_id("trig", inst, o.get("decision_id") or "-", side, sz, X._px(inst, px), order_px, settings.now_str()[:16])
    att = _attach(inst, o.get("stop_loss"), o.get("take_profit"), cl)
    body = {"instId": inst, "tdMode": "isolated", "side": "buy" if side == "long" else "sell", "ordType": "trigger", "sz": sz,
            "triggerPx": X._px(inst, px), "orderPx": order_px, "triggerPxType": ecfg(cfg)["trigger_px_type"], "algoClOrdId": cl,
            "attachAlgoOrds": [att]}
    if ps != "net":
        body["posSide"] = ps
    d = X.post("/api/v5/trade/order-algo", body)["data"][0]
    meta = {k: o.get(k) for k in TRIGGER_META_KEYS}
    meta.update(inst_id=inst, algoId=d["algoId"], algoClOrdId=cl, sz=sz, created=settings.now_str(), expires=expires)
    meta["order_px"] = order_px
    meta.update(extra or {})
    r = reg(); r["triggers"][d["algoId"]] = meta; save_reg(r)
    from desk import entry_state
    entry_state.advance(d["algoId"], "armed", inst_id=inst, decision_id=o.get("decision_id"))
    X.log_event("trigger_placed", **{k: meta[k] for k in ("inst_id", "algoId", "sz", "side", "trigger_px", "stop_loss", "take_profit", "decision_id", "order_px")})
    if order_px != "-1":   # 限价入场：登记到超时跟踪（desk/entry_limit.py，由 demo_watch 每轮检查）
        from desk import entry_limit
        entry_limit.register(d["algoId"], inst=inst, side=side, pos_side=ps, sz=sz, order_px=order_px, trigger_px=px,
                             stop_loss=o.get("stop_loss"), take_profit=o.get("take_profit"),
                             attach_cl=att["attachAlgoClOrdId"], lever=lev, decision_id=o.get("decision_id"),
                             **({"add": dict(extra["add"])} if (extra or {}).get("add") else {}))
    try:   # 挂单即时提醒（与开平仓同一 webhook 通道；按 algoId 去重，entry_limit_registered 不再另发）
        from desk.demo_ws import notify_placed
        notify_placed(meta)
    except Exception:
        pass
    return meta


def close(cfg, p, reason):
    _require()
    from desk import exec_lock
    with exec_lock.hold(p["inst_id"], why="close"):
        return _close(cfg, p, reason)


def _close(cfg, p, reason):
    body = {"instId": p["inst_id"], "mgnMode": "isolated", "autoCxl": True}
    if p.get("pos_side", "net") != "net":
        body["posSide"] = p["pos_side"]
    X.post("/api/v5/trade/close-position", body)
    r = reg(); r["positions"].pop(p["inst_id"], None); save_reg(r)
    X.log_event("close_ok", inst_id=p["inst_id"], pos_side=p.get("pos_side"), reason=reason)
    return {"type": "close", "ok": True, "trade_id": p["trade_id"], "px": p["price"], "pnl": p["upnl"]}


def _replace_close_algos(p, sl, tp, sz):
    """按新张数重挂止损止盈：先挂新的并确认可见，再撤旧的（避免中间出现裸仓）；新单失败保留旧单并告警。"""
    inst = p["inst_id"]
    mine = [a for a in close_algos(inst) if a.get("posSide") == p.get("pos_side")]
    body = {"instId": inst, "tdMode": "isolated", "side": "sell" if p["side"] == "long" else "buy",
            "ordType": "oco" if (sl and tp) else "conditional", "sz": sz, "reduceOnly": True,
            "algoClOrdId": det_id("rc", inst, p.get("pos_side", "net"), sl, tp, sz, settings.now_str()[:16])}
    if p.get("pos_side", "net") != "net":
        body["posSide"] = p["pos_side"]
    if sl:
        body.update(slTriggerPx=X._px(inst, sl), slOrdPx="-1")
    if tp:
        body.update(tpTriggerPx=X._px(inst, tp), tpOrdPx="-1")
    try:
        new_id = X.post("/api/v5/trade/order-algo", body)["data"][0].get("algoId")
    except Exception as e:
        _alert("stop_replace_failed", inst_id=inst, step="replace_place_new", error=f"{type(e).__name__}: {e}"[:300],
               kept=[a["algoId"] for a in mine])
        return None
    if not _algo_live(inst, new_id):
        _alert("stop_replace_failed", inst_id=inst, step="replace_confirm_new", algoId=new_id, kept=[a["algoId"] for a in mine])
        return None
    if mine:
        X.post("/api/v5/trade/cancel-algos", [{"instId": inst, "algoId": a["algoId"]} for a in mine])
    return new_id


def reduce(cfg, p, fraction, reason):
    _require()
    from desk import exec_lock
    with exec_lock.hold(p["inst_id"], why="reduce"):
        return _reduce(cfg, p, fraction, reason)


def _reduce(cfg, p, fraction, reason, restitch_protection: bool = True):
    """restitch_protection=False：只市价减仓，不重挂/不撤保护单（加仓腿复核减仓用，避免 consolidate 原仓保护）。"""
    lot = float(X.instrument(p["inst_id"])["lotSz"])
    part = math.floor(p["contracts"] * fraction / lot + 1e-9) * lot
    if part <= 0:
        return {"type": "reduce", "ok": False, "reason": "减仓张数不足"}
    body = {"instId": p["inst_id"], "tdMode": "isolated", "side": "sell" if p["side"] == "long" else "buy", "ordType": "market",
            "sz": X._dec(part), "reduceOnly": True}
    if p.get("pos_side", "net") != "net":
        body["posSide"] = p["pos_side"]
    X.post("/api/v5/trade/order", body)
    if restitch_protection:
        _replace_close_algos(p, p.get("sl_px"), p.get("tp_px"), X._dec(p["contracts"] - part))
    X.log_event("reduce_ok", inst_id=p["inst_id"], sz=X._dec(part), reason=reason, restitch=restitch_protection)
    return {"type": "reduce", "ok": True, "trade_id": p["trade_id"], "px": p["price"], "pnl": p["upnl"] * fraction,
            "remaining": float(X._dec(p["contracts"] - part))}   # 剩余张数：减仓后改止损时按此张数挂单


def move_stop(cfg, p, new_sl):
    """只允许收紧：多单止损只能上移，空单只能下移。先试 amend；交易所不支持修改（51506）时
    改为先挂新的 reduceOnly 止损止盈（保留原止盈）、确认生效后再撤旧单（tighten_by_replace）。"""
    _require()
    from desk import exec_lock
    with exec_lock.hold(p["inst_id"], why="move_stop"):
        old = p.get("sl_px")
        if old and (new_sl < old if p["side"] == "long" else new_sl > old) and not cfg["manager"].get("allow_stop_widen"):
            return False, f"禁止放宽止损（{old:.6g}→{new_sl:.6g}）"
        inst = p["inst_id"]
        mine = [a for a in close_algos(inst) if a.get("posSide") == p.get("pos_side") and a.get("slTriggerPx")]
        if not mine:
            return tighten_by_replace(cfg, p, new_sl)
        try:
            X.post("/api/v5/trade/amend-algos", {"instId": inst, "algoId": mine[0]["algoId"],
                                                  "newSlTriggerPx": X._px(inst, new_sl), "newSlOrdPx": "-1"})
        except X.DemoError as e:
            if "51506" not in str(e):
                raise
            X.log_event("stop_amend_unsupported", inst_id=inst, algoId=mine[0]["algoId"])
            return tighten_by_replace(cfg, p, new_sl)
        X.log_event("stop_ok", inst_id=inst, old=old, new_sl=new_sl)
        return True, f"模拟盘止损 {old}→{new_sl:.6g}"


def move_tp(cfg, p, new_tp):
    """交易经理 new_tp：改同一张止损止盈单的止盈价（可调近或调远），止损不动。
    新止盈须在现价盈利一侧（多单高于现价、空单低于现价）且距现价至少 manager.tp_min_gap_pct%，否则拒绝。
    优先 amend 原单；原单只有止损（conditional）、张数与持仓不一致或交易所不支持修改时，先挂新 oco（止损不变、张数=持仓）再撤旧。
    没有止损单时不单独挂止盈（避免出现无止损的保护单），返回 (ok, msg)。"""
    _require()
    from desk import exec_lock, okx_data
    inst = p["inst_id"]
    with exec_lock.hold(inst, why="move_tp"):
        try:
            last = float(okx_data.last_price(inst))
        except Exception:
            last = float(p.get("price") or 0)
        if not last:
            return False, "取不到现价，new_tp 未执行"
        gap = float((cfg.get("manager") or {}).get("tp_min_gap_pct", 0.3)) / 100
        long = p["side"] == "long"
        if (new_tp <= last * (1 + gap)) if long else (new_tp >= last * (1 - gap)):
            return False, f"new_tp {new_tp:.6g} 不在现价 {last:.6g} 盈利一侧或距离不足 {gap * 100:g}%，未改"
        mine = [a for a in close_algos(inst) if a.get("posSide") == p.get("pos_side", "net") and a.get("slTriggerPx")]
        if not mine:
            return False, "没有止损单，不单独挂止盈，new_tp 未执行"
        a = mine[0]
        old = float(a["tpTriggerPx"]) if a.get("tpTriggerPx") else None
        sz = X._dec(p["contracts"])
        if a.get("tpTriggerPx") and float(a.get("sz") or 0) == float(p["contracts"]):
            try:
                X.post("/api/v5/trade/amend-algos", {"instId": inst, "algoId": a["algoId"],
                                                      "newTpTriggerPx": X._px(inst, new_tp), "newTpOrdPx": "-1"})
                X.log_event("tp_ok", inst_id=inst, algoId=a["algoId"], old_tp=old, new_tp=new_tp, how="amend")
                return True, f"模拟盘止盈 {old}→{new_tp:.6g}（改原单 {a['algoId']}）"
            except X.DemoError as e:
                if "51506" not in str(e):
                    return False, f"模拟盘改止盈失败：{e}"
        new_id = _replace_close_algos(p, float(a["slTriggerPx"]), new_tp, sz)
        if not new_id:
            return False, "新止盈止损单挂单或确认失败，保留旧单"
        X.log_event("tp_ok", inst_id=inst, old_tp=old, new_tp=new_tp, how="replace", new_algoId=new_id, sz=sz)
        return True, f"模拟盘止盈（先挂新再撤旧）{old}→{new_tp:.6g}，新单 {new_id}，张数 {sz}"


def _alert(kind, **kw):
    X.log_event(kind, **kw)
    try:
        from desk.demo_ws import notify
        notify({"kind": kind, **kw})
    except Exception:
        pass


def _algo_live(inst, algo_id, tries=10, wait=0.5) -> bool:
    import time as _t
    for i in range(tries):
        if any(str(a.get("algoId")) == str(algo_id) for a in close_algos(inst)):
            return True
        if i < tries - 1:
            _t.sleep(wait)
    return False


def tighten_by_replace(cfg, p, new_sl, tp=None, cl=None):
    """B2/R11.4：先挂新的 reduceOnly 止损止盈（张数=持仓张数，保留原止盈），确认在 orders-algo-pending 中可见后再撤旧单。
    只能收紧（不看 allow_stop_widen）。新单失败 → 保留旧单并告警 stop_replace_failed；旧单撤不掉 → 告警 stop_replace_old_left。
    返回 (ok, msg)。调用方须持该币执行锁（本函数自身也会拿，可重入）。"""
    from desk import exec_lock
    inst = p["inst_id"]
    with exec_lock.hold(inst, why="tighten_by_replace"):
        olds = [a for a in close_algos(inst) if a.get("posSide") == p.get("pos_side", "net") and a.get("slTriggerPx")]
        cur = [float(a["slTriggerPx"]) for a in olds]
        if cur:
            tight = max(cur) if p["side"] == "long" else min(cur)
            if (new_sl <= tight) if p["side"] == "long" else (new_sl >= tight):
                return False, f"新止损 {new_sl:.6g} 不比现有止损 {tight:.6g} 更紧，未改"
        last = float(p.get("price") or 0)
        if last and ((new_sl >= last) if p["side"] == "long" else (new_sl <= last)):
            return False, f"新止损 {new_sl:.6g} 已越过现价 {last:.6g}，未改"
        if tp is None:
            tps = [float(a["tpTriggerPx"]) for a in olds if a.get("tpTriggerPx")]
            tp = tps[0] if tps else p.get("tp_px")
        sz = X._dec(p["contracts"])
        cl = cl or det_id("sl", inst, p.get("pos_side", "net"), X._px(inst, new_sl), settings.now_str()[:16])
        body = {"instId": inst, "tdMode": "isolated", "side": "sell" if p["side"] == "long" else "buy",
                "ordType": "oco" if tp else "conditional", "sz": sz, "reduceOnly": True, "algoClOrdId": cl,
                "slTriggerPx": X._px(inst, new_sl), "slOrdPx": "-1", "slTriggerPxType": "last"}
        if p.get("pos_side", "net") != "net":
            body["posSide"] = p["pos_side"]
        if tp:
            body.update(tpTriggerPx=X._px(inst, tp), tpOrdPx="-1", tpTriggerPxType="last")
        try:
            new_id = X.post("/api/v5/trade/order-algo", body)["data"][0].get("algoId")
        except Exception as e:
            _alert("stop_replace_failed", inst_id=inst, step="place_new", error=f"{type(e).__name__}: {e}"[:300],
                   kept=[a["algoId"] for a in olds])
            return False, f"新止损单挂单失败，保留旧单：{type(e).__name__}"
        X.log_event("stop_replace_new_placed", inst_id=inst, algoId=new_id, algoClOrdId=cl, sl=new_sl, tp=tp, sz=sz)
        if not _algo_live(inst, new_id):
            _alert("stop_replace_failed", inst_id=inst, step="confirm_new", algoId=new_id, kept=[a["algoId"] for a in olds])
            return False, "新止损单未确认生效，保留旧单"
        left = []
        if olds:
            try:
                X.post("/api/v5/trade/cancel-algos", [{"instId": inst, "algoId": a["algoId"]} for a in olds])
            except Exception as e:
                X.log_event("stop_replace_cancel_error", inst_id=inst, error=f"{type(e).__name__}: {e}"[:300])
            now = {str(a.get("algoId")) for a in close_algos(inst)}
            left = [a["algoId"] for a in olds if str(a["algoId"]) in now]
            if left:
                _alert("stop_replace_old_left", inst_id=inst, algoIds=left, new_algoId=new_id)
        X.log_event("stop_ok", inst_id=inst, old=cur[0] if cur else None, new_sl=new_sl, how="replace",
                    new_algoId=new_id, cancelled=[a["algoId"] for a in olds if a["algoId"] not in left])
        return True, f"模拟盘止损（先挂新再撤旧）{cur[0] if cur else None}→{new_sl:.6g}，新单 {new_id}"


def det_id(*parts) -> str:
    """R11.1 确定性 clOrdId/algoClOrdId：ta + sha1(各部分)[:28]，只含字母数字，≤32 位。"""
    import hashlib
    return "ta" + hashlib.sha1("|".join(str(x) for x in parts).encode()).hexdigest()[:28]


def cancel_trigger(inst, algo_id, why) -> str:
    """撤指定的一张条件入场单（调用方明确指定 algoId；不看 protected_algo_ids，由调用方决定是否允许）。
    返回撤单后在算法单历史里的状态（canceled / effective / ...）；同时从登记表移除。"""
    _require()
    X.post("/api/v5/trade/cancel-algos", [{"instId": inst, "algoId": str(algo_id)}])
    state = "unknown"
    for _ in range(10):
        h = X.get("/api/v5/trade/orders-algo-history", ordType="trigger", algoId=str(algo_id)).get("data", [])
        if h:
            state = h[0].get("state"); break
        import time; time.sleep(0.5)
    r = reg(); r["triggers"].pop(str(algo_id), None); save_reg(r)
    X.log_event("trigger_cancelled", inst_id=inst, algoId=str(algo_id), why=why, state=state)
    return state
