"""交易经理受限下单工具（2026-10-31 用户决定；交易经理所有入口固定走本工具，无开关）。

做法：交易经理在讨论汇总之后，按固定参数「调用」一次下单工具（严格 JSON schema 结构化输出）；代码只做两件事：
1) 参数校验 + 硬护栏预检（沿用 demo_exec.check_open、sizing、stop_cooldown、drift 等现有函数，不另写一套）；
   不通过 → 把具体原因与允许范围原样返回交易经理，让它当场改参数重试（最多 TOOL_MAX_ATTEMPTS 次）；
2) 通过 → 原样映射为 ManagerDecision，交给现有执行层（组合经理 _pm_execute_demo_locked 及其背后的
   open_now / place_trigger / place_add_trigger / keep_same_trigger / cancel_triggers / apply_tm_expiry）执行。
不做关键词识别、不静默改数：交易经理给的点位、张数（margin_pct）、risk_scale、保留/重挂以其参数为准，只受硬护栏封顶。
理由文字只进复盘日报，不参与执行。只对 OKX 模拟盘（demo）下单，本模块不含任何实盘下单逻辑。"""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field

TOOL_MAX_ATTEMPTS = 3          # 沿用项目「模型调用失败重试 3 次」约定
OPEN_ACTS = ("open", "add", "pending", "replace_pending")
TRIGGER_ACTS = ("pending", "replace_pending", "keep_pending")


class OrderToolCall(BaseModel):
    """交易经理下单工具的参数（固定、受限）。"""
    action: Literal["open", "add", "pending", "keep_pending", "replace_pending", "cancel_pending", "hold", "close", "reduce"] = Field(
        description="open=无持仓现价开仓；add=有持仓同向现价加仓；pending=挂新条件入场单（无现有挂单时）；"
                    "keep_pending=保留现有挂单（不撤、张数不变，只按你给的止损止盈/有效期更新）；replace_pending=撤现有挂单按你给的参数重挂；"
                    "cancel_pending=撤掉现有挂单不再挂；hold=不开仓不动挂单（有持仓时可用 new_stop/new_tp 调点）；close=平掉持仓；reduce=部分减仓")
    side: Literal["long", "short"] | None = Field(default=None, description="方向：open/add/pending/keep_pending/replace_pending 必填，必须与共识方向一致")
    entry: float | None = Field(default=None, description="open/add 必填：现价入场价（写当前价）")
    trigger_px: float | None = Field(default=None, description="pending/replace_pending/keep_pending 必填：触发价（keep_pending 填现有挂单触发价）")
    condition: Literal["gte", "lte"] | None = Field(default=None, description="pending/replace_pending 必填：gte=涨到≥触发价开仓，lte=跌到≤触发价开仓")
    stop_loss: float | None = Field(default=None, description="开仓/加仓/挂单类必填：止损价（做多在下方、做空在上方，距入场≤8%）")
    take_profit: float | None = Field(default=None, description="开仓/加仓/挂单类必填：止盈价，盈亏比≥1.5")
    margin_pct: float | None = Field(default=None, description="可选：本单保证金占权益%（定张数，以你为准，只受硬护栏封顶）；留空=按单笔风险定仓×risk_scale")
    risk_scale: float = Field(default=1.0, description="0~1：未给 margin_pct 时按风险定仓的缩放")
    leverage: float = Field(default=1, description="杠杆（只作上限，新开仓上限由代码强制）")
    expire_hours: float | None = Field(default=None, description="pending/replace_pending/keep_pending 必填（keep_expiry=true 时可不填）：有效期小时，"
                                       "从挂单时刻起算：pending/replace_pending 从本次新挂单时刻起算；keep_pending 从现有挂单的原挂单时刻起算（不是从现在起算）")
    keep_expiry: bool = Field(default=False, description="仅 keep_pending/replace_pending 可用：true=沿用现有挂单原到期时刻，忽略 expire_hours")
    new_stop: float | None = Field(default=None, description="hold/reduce 有持仓时可选：新止损（只能收紧）")
    new_tp: float | None = Field(default=None, description="hold/reduce 有持仓时可选：新止盈（须在现价盈利一侧）")
    reduce_fraction: float | None = Field(default=None, description="reduce 必填：减仓比例 0.1~0.9")
    horizon_days: int = Field(default=5, description="持有周期天数 1~30")
    invalidation: str = Field(default="", description="必填：什么情况说明判断错了")
    rationale: str = Field(default="", description="必填：中文理由（只进复盘日报，不参与执行）")


REQUIRED = {"open": ("side", "entry", "stop_loss", "take_profit"),
            "add": ("side", "entry", "stop_loss", "take_profit"),
            "pending": ("side", "trigger_px", "condition", "stop_loss", "take_profit", "expire_hours"),
            "replace_pending": ("side", "trigger_px", "condition", "stop_loss", "take_profit", "expire_hours"),
            "keep_pending": ("side", "trigger_px", "stop_loss", "take_profit", "expire_hours"),
            "reduce": ("reduce_fraction",), "close": (), "hold": (), "cancel_pending": ()}


def required(call) -> tuple:
    """keep_expiry=true 的 keep_pending/replace_pending 不要求 expire_hours（沿用原到期时刻）。"""
    r = REQUIRED[call.action]
    return tuple(k for k in r if k != "expire_hours") if call.keep_expiry and call.action in ("keep_pending", "replace_pending") else r


def _parse_ts(v):
    import datetime as _dt
    if not v:
        return None
    for f in ("%Y-%m-%d %H:%M:%S%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return _dt.datetime.strptime(str(v), f)
        except ValueError:
            pass
    try:
        return _dt.datetime.fromisoformat(str(v))
    except ValueError:
        return None


def remaining_hours(expires) -> float | None:
    """现有挂单原到期时刻距现在的小时数；无到期或已过期返回 None。"""
    from desk import settings
    t = _parse_ts(expires)
    if t is None:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=settings.now().tzinfo)
    h = (t - settings.now()).total_seconds() / 3600
    return h if h > 0 else None


def _num(v):
    try:
        return float(v) if v not in (None, "") and float(v) > 0 else None
    except (TypeError, ValueError):
        return None


def validate(cfg, call: OrderToolCall, ctx: dict) -> list[str]:
    """返回拒绝原因列表（空=通过）。ctx: inst, mk, bk, pos, consensus_side, existing（现有条件单 dict 或 None）。
    只用现有函数做硬护栏预检，不改任何参数。"""
    from desk import sizing
    errs: list[str] = []
    a, pos, ex, cs = call.action, ctx.get("pos"), ctx.get("existing"), ctx.get("consensus_side")
    mk, bk, inst = ctx["mk"], ctx["bk"], ctx["inst"]
    rc = sizing.rcfg(cfg)
    # 1) 必填项
    miss = [k for k in required(call) if (getattr(call, k) in (None, "") if k in ("side", "condition") else _num(getattr(call, k)) is None)]
    if not (call.invalidation or "").strip():
        miss.append("invalidation")
    if not (call.rationale or "").strip():
        miss.append("rationale")
    if miss:
        errs.append(f"[MISSING] action={a} 缺必填参数：{'、'.join(miss)}（必须给具体正数，不能留空）")
        return errs
    # 2) 动作与账户状态
    if a == "open" and pos:
        errs.append("[STATE] 该币已有持仓，不能 open；同向加仓请用 add，或 hold/close/reduce")
    if a == "add" and not pos:
        errs.append("[STATE] 该币没有持仓，不能 add；请用 open 或 pending")
    if a in ("close", "reduce") and not pos:
        errs.append(f"[STATE] 该币没有持仓，不能 {a}")
    if a in ("keep_pending", "replace_pending", "cancel_pending") and not ex:
        errs.append(f"[STATE] 该币没有现有条件挂单，不能 {a}；新挂单请用 pending")
    if ex and a in ("pending", "hold", "open"):
        errs.append(f"[STATE] 该币有现有条件挂单 {ex.get('algoId')}（{ex.get('side')} 触发价 {ex.get('trigger_px')}，"
                    f"{ex.get('sz')} 张）：必须明确 keep_pending（保留）、replace_pending（撤旧重挂）或 cancel_pending（撤单），"
                    "或 close/reduce 处理持仓")
    if a == "keep_pending" and ex and _num(call.trigger_px) and not sizing.same_px(
            float(call.trigger_px), float(ex["trigger_px"]), float(sizing.scfg(cfg)["same_px_tol_pct"])):
        errs.append(f"[KEEP] keep_pending 的 trigger_px 须等于现有挂单触发价 {ex['trigger_px']}；改价位请用 replace_pending")
    if call.keep_expiry and a not in ("keep_pending", "replace_pending"):
        errs.append(f"[EXPIRY] keep_expiry=true 只用于 keep_pending/replace_pending（沿用现有挂单原到期时刻）；{a} 请改为 false")
    elif call.keep_expiry and ex and remaining_hours(ex.get("expires")) is None:
        errs.append(f"[EXPIRY] 现有挂单原到期时刻 {ex.get('expires') or '未登记'} 无法沿用（未登记或已过期）；请设 keep_expiry=false 并给 expire_hours")
    if a == "reduce" and call.reduce_fraction is not None and not (0.1 <= float(call.reduce_fraction) <= 0.9):
        errs.append(f"[REDUCE] reduce_fraction={call.reduce_fraction} 超出允许范围 0.1~0.9（全平请用 close）")
    if errs:
        return errs
    # 3) 方向：交易经理不能翻转共识
    if a in OPEN_ACTS or a == "keep_pending":
        if cs is None:
            errs.append("[DIRECTION] 当前加权共识无方向，不能开仓/挂单；只能 hold、cancel_pending、close、reduce")
        elif call.side != cs:
            errs.append(f"[DIRECTION] side={call.side} 与共识方向 {cs} 相反：交易经理不能翻转多空，只能按共识方向下单或 hold/否决")
        if pos and call.side != pos.get("side"):
            errs.append(f"[DIRECTION] 现有持仓方向 {pos.get('side')}，不反手（须先 close）")
        if errs:
            return errs
    # 4) 点位硬护栏（止损侧向、距离≤stop_max_pct、止盈侧向、RR≥min_rr）
    if a in OPEN_ACTS or a == "keep_pending":
        px = float(mk["last"]) if a in ("open", "add") else float(call.trigger_px)
        sl, tp, sg = float(call.stop_loss), float(call.take_profit), (1 if call.side == "long" else -1)
        smax, min_rr = float(rc["stop_max_pct"]), float(rc.get("min_rr_after_adjust", 1.5))
        lo, hi = (px * (1 - smax), px) if sg > 0 else (px, px * (1 + smax))
        if sg * (px - sl) <= 0:
            errs.append(f"[STOP_SIDE] 止损 {sl:g} 在错误一侧（{'做多须低于' if sg > 0 else '做空须高于'}入场 {px:g}）；允许区间 {lo:.6g}~{hi:.6g}")
        elif abs(px - sl) / px > smax + 1e-12:
            errs.append(f"[STOP_MAX] 止损距入场 {abs(px - sl) / px:.2%} 超过上限 {smax:.0%}；允许区间 {lo:.6g}~{hi:.6g}")
        if sg * (tp - px) <= 0:
            errs.append(f"[TP_SIDE] 止盈 {tp:g} 不在盈利一侧（入场 {px:g}）")
        if not errs:
            rr = sizing.rr_ratio(call.side, px, sl, tp)
            if rr is None or rr + 1e-12 < min_rr:
                need = px + sg * min_rr * abs(px - sl)
                errs.append(f"[RR] 盈亏比 {('无效' if rr is None else f'{rr:.2f}')} < {min_rr:g}；按此止损，止盈至少 {need:.6g}（或收紧止损）")
        if errs:
            return errs
    # 5) 冷却、价格漂移（现价单）
    if a in OPEN_ACTS or a == "keep_pending":
        from desk import stop_cooldown
        b = stop_cooldown.blocked(cfg, inst, call.side)
        if b:
            errs.append(f"[COOLDOWN] {inst} {call.side} 方向 {b['since_h']} 小时前被止损，冷却还剩 {b['left_h']} 小时，同向不能开仓/挂单")
    if a in ("open", "add"):
        e = drift_error(cfg, inst, mk)
        if e:
            errs.append(e)
    if errs:
        return errs
    # 6) 组合层硬护栏：check_open（保证金 33%、总名义 100%、同向净名义、单笔风险 1.5%、总在途风险 4%、日亏、KILL/HALT、最多 3 仓每币一仓）
    if a in OPEN_ACTS:
        errs += guard_errors(cfg, call, ctx)
    elif a == "keep_pending":
        errs += keep_errors(cfg, call, ctx)
    elif a in ("hold", "reduce") and pos:
        errs += position_adjust_errors(cfg, call, pos, mk)
    return errs


def drift_error(cfg, inst, mk) -> str | None:
    from desk import drift, okx_data
    c = drift.dcfg(cfg)
    ref = (mk or {}).get("_analysis_ref") or {}
    if not c["enabled"] or not ref.get("px"):
        return None
    try:
        now_px = float(okx_data.last_price(inst))
    except Exception:
        return None   # 与现有 drift.skip 一致：取不到现价不阻断（执行层还会再查一次）
    dev = (now_px - float(ref["px"])) / float(ref["px"]) * 100
    if abs(dev) > float(c["max_pct"]):
        return (f"[DRIFT] 分析时 {float(ref['px']):.6g} → 现价 {now_px:.6g}，偏离 {dev:+.2f}% 超过 ±{float(c['max_pct']):g}%：现价单不下；"
                "可改用 pending 挂条件单或 hold")
    return None


def _qty(cfg, call, ctx, px, sl):
    """张数：给了 margin_pct 以交易经理为准；否则按现有 demo_size（单笔风险×risk_scale）。返回 (qty_coin, contracts, how) 或错误串。"""
    from desk import manager, okx_demo as X, config_safety
    bk, inst = ctx["bk"], ctx["inst"]
    lev = float(min(max(float(call.leverage or 1), 1), config_safety.max_open_leverage(cfg)))
    if _num(call.margin_pct):
        eq = float(bk.get("live_equity") or bk["equity"])
        notional = float(call.margin_pct) / 100 * eq * lev
        how = f"交易经理 margin_pct={float(call.margin_pct):g}%"
    else:
        agg = {"side": call.side, "entry_px": px, "stop_loss": sl, "S": ctx.get("S") or 0.0}
        r = manager.demo_size(cfg, agg, ctx["mk"], bk, to_decision(call, ctx), inst)
        if not r.get("ok"):
            return f"[SIZING] 按风险定仓失败：{r.get('reason')}"
        notional, lev = float(r["notional"]), float(r.get("leverage") or lev)
        how = f"按风险定仓×risk_scale {float(call.risk_scale):g}"
    sz = X.contracts(inst, notional / px)
    if not sz:
        return "[SIZE] 不足最小张数，请提高 margin_pct 或 hold"
    qty = float(sz) * float(X.instrument(inst)["ctVal"])
    return qty, sz, lev, how


def allowed_range(cfg, ctx, px, sl) -> str:
    from desk import demo_exec as DX
    g = cfg["guardrails"]
    eq = float(ctx["bk"]["equity"])
    dist = abs(px - sl) / px
    by_risk = DX.risk_cap_usd(cfg, eq) / dist / eq * 100 if dist > 0 else 0
    return f"允许 margin_pct ≤ {min(float(g['max_position_pct']), by_risk):.2f}%（单笔保证金上限 {g['max_position_pct']}%，按此止损单笔风险上限约 {by_risk:.2f}%）"


def guard_errors(cfg, call, ctx) -> list[str]:
    from desk import demo_exec as DX
    if not DX.is_demo(cfg):   # 纸面模式（paper）不对接 OKX 合约规格，护栏由 pm_execute 纸面路径自行执行
        return []
    bk, inst, pos = ctx["bk"], ctx["inst"], ctx.get("pos")
    px = float(ctx["mk"]["last"]) if call.action in ("open", "add") else float(call.trigger_px)
    sl = float(call.stop_loss)
    q = _qty(cfg, call, ctx, px, sl)
    if isinstance(q, str):
        return [q]
    qty, sz, lev, how = q
    bk2 = bk if call.action in ("open", "add") else {**bk, "triggers": [t for t in bk.get("triggers") or [] if t.get("instId") != inst]}
    reasons, _checks = DX.check_open(cfg, bk2, inst, qty, px, sl, call.side, lev, add=pos if pos else None)
    rc = receipt(bk, sz, qty, px, sl, lev, how)
    if not reasons:
        ctx.setdefault("_sized", {})[call.action] = {"sz": sz, "qty": qty, "leverage": lev, "how": how, **rc}
        return []
    return [f"[GUARD] {r}" for r in reasons] + [f"[RANGE] 本次 {receipt_text(rc)}；{allowed_range(cfg, ctx, px, sl)}"]


def receipt(bk, sz, qty, px, sl, lev, how) -> dict:
    """预检回执：张数、名义、保证金、止损风险金额与占权益%（止损风险=张数×面值×|入场−止损|，不含手续费滑点）。"""
    eq = float(bk.get("live_equity") or bk.get("equity") or 0)
    notional = float(qty) * float(px)
    risk = float(qty) * abs(float(px) - float(sl))
    lev = float(lev or 1)
    return {"how": how, "sz": sz, "notional": round(notional, 2), "margin": round(notional / lev, 2), "leverage": lev,
            "stop_loss_usd": round(risk, 2), "risk_pct_equity": round(risk / eq * 100, 3) if eq > 0 else None,
            "margin_pct_equity": round(notional / lev / eq * 100, 3) if eq > 0 else None}


def receipt_text(r: dict) -> str:
    if not r:
        return ""
    pct = lambda v: "—" if v is None else f"{v:.2f}%"
    return (f"{r.get('how')} → {r.get('sz')} 张，名义 {r.get('notional'):,.2f} USDT，保证金 {r.get('margin'):,.2f} USDT"
            f"（占权益 {pct(r.get('margin_pct_equity'))}，杠杆 {r.get('leverage'):g}x），止损亏损约 {r.get('stop_loss_usd'):,.2f} USDT"
            f"（占权益 {pct(r.get('risk_pct_equity'))}）")


def keep_errors(cfg, call, ctx) -> list[str]:
    """保留现有挂单：按原张数，用现有 sizing.pending_keep_sltp（与执行层同一函数）预检交易经理的止损止盈，任何「保持原值」都返回给交易经理。"""
    from desk import manager, sizing
    ex, mk, rc, M = ctx["existing"], ctx["mk"], sizing.rcfg(cfg), cfg.get("manager") or {}
    r = sizing.pending_keep_sltp(ex["side"], ex["trigger_px"], ex.get("stop_loss"), ex.get("take_profit"),
                                 float(call.stop_loss), float(call.take_profit), float(mk["last"]),
                                 float(rc.get("min_rr_after_adjust", 1.5)), float(M.get("tp_min_gap_pct", 0.3)),
                                 allow_widen=True, stop_max_pct=float(rc["stop_max_pct"]),
                                 widen_check=manager.pending_widen_guard(cfg, ctx["inst"], ex, ctx["bk"]))
    bad = [x for x in r["reasons"] if "保持原" in x or "弃用" in x]
    if not bad:
        try:   # 保留挂单的回执：原张数 × 新止损
            from desk import demo_exec as DX, okx_demo as X
            if DX.is_demo(cfg) and ex.get("sz"):
                qty = float(ex["sz"]) * float(X.instrument(ctx["inst"])["ctVal"])
                ctx.setdefault("_sized", {})["keep_pending"] = receipt(ctx["bk"], ex["sz"], qty, float(ex["trigger_px"]),
                                                                      float(call.stop_loss), call.leverage, "保留原挂单张数")
        except Exception:
            pass
    return [f"[KEEP_GUARD] {x}（原挂单 {ex.get('sz')} 张不变）" for x in bad]


def position_adjust_errors(cfg, call, pos, mk) -> list[str]:
    from desk import sizing
    errs, sg, last = [], (1 if pos["side"] == "long" else -1), float(mk["last"])
    cur = _num(pos.get("sl_px"))
    if _num(call.new_stop) is not None:
        ns = float(call.new_stop)
        if sg * (last - ns) <= 0:
            errs.append(f"[NEW_STOP] new_stop {ns:g} 在现价 {last:g} 错误一侧（会立即触发）")
        elif cur is not None and sg * (ns - cur) < 0:
            errs.append(f"[NEW_STOP] 已有持仓止损只能收紧：现止损 {cur:g}，{'多单只能上移' if sg > 0 else '空单只能下移'}（允许 {'≥' if sg > 0 else '≤'} {cur:g}）")
    if _num(call.new_tp) is not None and sg * (float(call.new_tp) - last) <= 0:
        errs.append(f"[NEW_TP] new_tp {float(call.new_tp):g} 不在现价 {last:g} 盈利一侧")
    if not errs and (_num(call.new_stop) or _num(call.new_tp)):
        stop = _num(call.new_stop) or pos.get("sl_px")
        tp = _num(call.new_tp) or pos.get("tp_px")
        ok, msg, _ = sizing.guard_rr(pos["side"], float(pos.get("avg_px") or 0), stop, tp,
                                     float(sizing.rcfg(cfg).get("min_rr_after_adjust", 1.5)))
        if not ok:
            errs.append(f"[RR] 调点后 {msg}")
    return errs


def to_decision(call: OrderToolCall, ctx: dict):
    """把通过校验的工具参数原样映射为现有执行层使用的 ManagerDecision（不改任何数）。"""
    from desk import manager
    a = call.action
    base = dict(risk_scale=float(call.risk_scale if call.risk_scale is not None else 1.0), leverage=float(call.leverage or 1),
                horizon_days=int(min(max(call.horizon_days or 5, 1), 30)), invalidation=call.invalidation,
                rationale=call.rationale, confidence=0.6)
    if a in ("open", "add"):
        d = dict(action="buy" if call.side == "long" else "short", entry=call.entry, stop_loss=call.stop_loss, take_profit=call.take_profit)
    elif a in TRIGGER_ACTS:
        ex = ctx.get("existing") or {}
        d = dict(action="pending", entry_side=call.side, entry_trigger_px=call.trigger_px,
                 entry_condition=call.condition or ex.get("condition"), entry_stop_loss=call.stop_loss,
                 entry_take_profit=call.take_profit, entry_size_pct=float(call.margin_pct) if _num(call.margin_pct) else 1.0,
                 entry_expire_hours=call.expire_hours)
        if call.keep_expiry and a in ("keep_pending", "replace_pending"):
            # keep_pending：不改到期（apply_tm_expiry 收到 None 即不动）；replace_pending：新单沿用原到期时刻（执行时按剩余小时重算）
            d["entry_expire_hours"] = None if a == "keep_pending" else remaining_hours(ex.get("expires"))
    elif a == "close":
        d = dict(action="sell")
    elif a == "reduce":
        d = dict(action="reduce", reduce_fraction=call.reduce_fraction, new_stop=call.new_stop, new_tp=call.new_tp)
    else:   # hold / cancel_pending
        d = dict(action="hold", new_stop=call.new_stop, new_tp=call.new_tp)
        if a == "cancel_pending":
            base["confidence"] = 0.0   # 明确否决 → 走现有否决路径撤单
    dec = manager.ManagerDecision(**{**base, **d})
    dec._order_tool = {"action": a, "margin_pct": _num(call.margin_pct), "call": call.model_dump(),
                       "keep_expiry_at": (ctx.get("existing") or {}).get("expires") if call.keep_expiry and a in ("keep_pending", "replace_pending") else None}
    return dec


def instructions(ctx: dict, cfg=None) -> str:
    ex, pos = ctx.get("existing"), ctx.get("pos")
    lines = ["", "## 下单工具（本次以工具参数为准，代码不再解析你的理由文字）",
             "讨论汇总之后，请按 OrderToolCall 的固定参数调用一次下单工具。代码只校验必填项与硬护栏，"
             "不会改你的点位、张数、保留/重挂决定；不通过会把原因与允许范围返回给你，请当场改参数重新调用"
             f"（最多 {TOOL_MAX_ATTEMPTS} 次，仍不通过按无结论不开仓）。",
             f"当前加权共识方向：{ctx.get('consensus_side') or '无方向'}（你不能翻转，只能按此方向下单或否决）。",
             "必填：open/add → side、entry、stop_loss、take_profit；pending/replace_pending → side、trigger_px、condition、stop_loss、"
             "take_profit、expire_hours；keep_pending → side、trigger_px（现有挂单触发价）、stop_loss、take_profit、expire_hours；"
             "reduce → reduce_fraction；所有动作都要 invalidation 与 rationale。",
             "margin_pct 可选：给了就按它定张数（只受硬护栏封顶，超出会拒绝并告诉你上限）；不给按单笔风险×risk_scale 定仓。",
             "有效期 expire_hours 从挂单时刻起算：pending/replace_pending 从本次新挂单时刻起算；keep_pending 从现有挂单的原挂单时刻起算（不是从现在起算）。"
             "想沿用现有挂单原到期时刻，就在 keep_pending/replace_pending 设 keep_expiry=true，此时忽略 expire_hours。",
             "预检通过就直接执行，不会再回给你确认；被拒绝时会把本次张数、名义、保证金、止损亏损金额与占权益%一并返回。"]
    lines += baseline_lines(cfg, ctx)
    if ex:
        lines.append(f"本币现有条件挂单：{ex.get('algoId')} {ex.get('side')} 触发价 {ex.get('trigger_px')} 止损 {ex.get('stop_loss')} "
                     f"止盈 {ex.get('take_profit')} {ex.get('sz')} 张，原挂单时刻 {ex.get('created') or '未登记'}，到期 {ex.get('expires')}"
                     f"（keep_pending 的 expire_hours 从原挂单时刻起算）。必须明确 keep_pending / replace_pending / cancel_pending（或 close/reduce）。")
    if pos:
        lines.append(f"本币现有持仓：{pos.get('side')} 均价 {pos.get('avg_px')} 止损 {pos.get('sl_px')} 止盈 {pos.get('tp_px')}；"
                     "止损只能收紧；同向加仓用 add（现价）或 pending（条件加仓）。")
    return "\n".join(lines)


def baseline_lines(cfg, ctx) -> list[str]:
    """按风险定仓的心算口径 + 本币当前基准张数示例（risk_scale=1/0.5，止损距离 2%/4%，含硬上限削减）。取不到数据只给公式。"""
    out = ["定仓口径：risk_scale 乘以单笔风险 1.5% 的基准得到张数，请按此心算。基准风险金额 = 权益 × 1.5% × 共识强度缩放 × 回撤缩放；"
           "名义 = 风险金额 × risk_scale ÷ (止损距离% + 0.2% 成本缓冲)；张数 = 名义 ÷ 入场价 ÷ 每张面值；再受单笔保证金、在途风险等硬上限削减。"]
    if not cfg:
        return out
    try:
        from types import SimpleNamespace
        from desk import manager, demo_exec as DX, okx_demo as X
        inst, mk, bk, cs = ctx["inst"], ctx["mk"], ctx["bk"], ctx.get("consensus_side")
        px = float(mk["last"])
        sg = 1 if cs != "short" else -1
        demo = DX.is_demo(cfg)
        ex = []
        for d in (0.02, 0.04):
            agg = {"side": cs or "long", "entry_px": px, "stop_loss": px * (1 - sg * d), "S": ctx.get("S") or 0.0}
            for rs in (1.0, 0.5):
                r = manager.demo_size(cfg, agg, mk, bk, SimpleNamespace(risk_scale=rs, leverage=1.0), inst)
                if not r.get("ok"):
                    continue
                n = float(r["notional"])
                q = f"{X.contracts(inst, n / px)} 张" if demo else f"{n / px:.6g} 币"
                ex.append(f"止损距离 {d:.0%}、risk_scale {rs:g} → {q}（名义 {n:,.0f} USDT，止损亏损约 {n * d:,.0f} USDT）")
                if rs == 1.0:
                    base = r.get("risk_usd")
        if ex:
            out.append(f"本币当前基准（入场 {px:g}，基准风险金额约 {float(base):,.0f} USDT）：" + "；".join(ex) + "。")
    except Exception:
        pass
    return out


def retry_prompt(prompt: str, history: list) -> str:
    if not history:
        return prompt
    parts = [prompt, "", "## 你之前的工具调用被拒绝，请改参数重新调用"]
    for i, h in enumerate(history, 1):
        parts.append(f"第 {i} 次参数：{json.dumps(h['call'], ensure_ascii=False) if h.get('call') else '（无法解析）'}")
        parts.append("拒绝原因：" + "；".join(h["errors"]))
    return "\n".join(parts)


def run(cfg, model, prompt: str, ctx: dict) -> dict:
    """多轮：调用 → 校验 → 拒绝原因返回 → 重试。返回 {"dec", "call", "attempts", "history", "absent_why"}。"""
    history = []
    for n in range(1, TOOL_MAX_ATTEMPTS + 1):
        try:
            out = model.invoke(retry_prompt(prompt, history))
            call = out if isinstance(out, OrderToolCall) else (OrderToolCall(**out) if isinstance(out, dict) and out else None)
            if call is None:
                raise ValueError("工具调用输出为空")
        except Exception as e:
            history.append({"call": None, "errors": [f"[PARSE] 工具参数无法解析（{type(e).__name__}: {str(e)[:160]}）"]})
            continue
        errs = validate(cfg, call, ctx)
        if not errs:
            return {"dec": to_decision(call, ctx), "call": call.model_dump(), "attempts": n, "history": history, "absent_why": None,
                    "receipt": (ctx.get("_sized") or {}).get(call.action)}
        history.append({"call": call.model_dump(), "errors": errs})
    last = "；".join(history[-1]["errors"]) if history else "无输出"
    return {"dec": None, "call": history[-1]["call"] if history else None, "attempts": TOOL_MAX_ATTEMPTS, "history": history,
            "absent_why": f"交易经理下单工具 {TOOL_MAX_ATTEMPTS} 次调用均未通过校验（最后原因：{last[:300]}）"}
