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
    margin_pct: float | None = Field(default=None, description="可选：本单保证金占权益%（定张数，以你为准，只受硬护栏封顶）；留空=按单笔风险定仓×risk_scale。"
                                      "keep_pending（保留挂单）不改张数，此字段不起作用，不要填")
    risk_scale: float = Field(default=1.0, description="0~1：未给 margin_pct 时按风险定仓的缩放。keep_pending（保留挂单）不改张数，此字段不起作用，不要填")
    leverage: float = Field(default=1, description="杠杆（只作上限，新开仓上限由代码强制）")
    expire_hours: float | None = Field(default=None, description="pending/replace_pending/keep_pending 必填（keep_expiry=true 时可不填）：有效期小时，"
                                       "从挂单时刻起算：pending/replace_pending 从本次新挂单时刻起算；keep_pending 从现有挂单的原挂单时刻起算（不是从现在起算）")
    keep_expiry: bool = Field(default=False, description="仅 keep_pending/replace_pending 可用：true=沿用现有挂单原到期时刻，忽略 expire_hours；若原到期早于下一场开场后 2 小时，系统会自动顺延到下一场开场后 2 小时")
    new_stop: float | None = Field(default=None, description="hold/reduce 有持仓时可选：新止损（只能收紧）")
    new_tp: float | None = Field(default=None, description="hold/reduce 有持仓时可选：新止盈（须在现价盈利一侧）")
    reduce_fraction: float | None = Field(default=None, description="reduce 必填：减仓比例 0.1~0.9")
    horizon_days: int = Field(default=5, description="持有周期天数 1~30")
    invalidation: str = Field(default="", description="必填：什么情况说明判断错了")
    rationale: str = Field(default="", description="必填：中文理由（只进复盘日报，不参与执行）。"
                           "reason/rationale 里的任何操作意图都不会被执行，要动作必须通过 action 字段（例如 reduce 或改 new_tp）")


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
    # 3.5) 趋势市护栏（2026-11-11 改版：判定来源为 LLM 行情判定，desk/regime_judge）：只在本场判定生效
    #      （方向与下单方向一致且强度 strong/moderate）时触发；判定失败/无趋势完全不触发。
    #      作用域 = 新入场 open/pending/replace_pending（keep_pending 沿用原单，不在此列）。
    tr = ctx.get("trend") or {}
    if tr.get("enabled") and tr.get("active") and a in ("open", "pending", "replace_pending") and call.side == tr.get("side"):
        tc = tr.get("cfg") or {}
        atr, last = float(mk.get("atr14") or 0), float(mk["last"])
        rs = float(call.risk_scale if call.risk_scale is not None else 1.0)
        rs_cap = float(tc.get("oversold_risk_scale", 0.5))
        if tr.get("downgrade"):
            if a == "open":
                errs.append("[TREND_OVERSOLD] 本场行情判定为末段衰竭（late_exhaustion）降级中：禁用现价 open，只许近价 pending"
                            f"（触发价距现价≤{float(tc.get('near_atr', 0.3)):g}×ATR14），且 risk_scale≤{rs_cap:g}")
            if rs > rs_cap + 1e-12:
                errs.append(f"[TREND_OVERSOLD] 本场行情判定为末段衰竭（late_exhaustion）降级中：risk_scale={rs:g} 超过降级上限 {rs_cap:g}，请改小后重试")
        if a in ("pending", "replace_pending") and atr > 0 and _num(call.trigger_px):
            cap = float(tc.get("near_atr", 0.3)) * atr
            dist = abs(float(call.trigger_px) - last)
            if dist > cap + 1e-9:
                errs.append(f"[TREND_DIST] 趋势市近价护栏（依本场 LLM 行情判定）：触发价 {float(call.trigger_px):g} 距现价 {last:g} 为 {dist:.6g}，"
                            f"超过上限 {float(tc.get('near_atr', 0.3)):g}×ATR14={cap:.6g}；允许触发价范围 "
                            f"{last - cap:.6g}~{last + cap:.6g}（gte 小反弹或 lte 跌破均可），请改近后重试，"
                            "或改用 open 现价入场，不要改成 hold 绕过")
        if errs:
            return errs
    # 3.6) 移动止损退出标记（2026-11-14 用户批准，desk/trailing_stop.py）：趋势市生效（方向一致、强度
    #      strong/moderate）、非末段衰竭的新入场（open，或无持仓时的 pending/replace_pending）→ 该仓位
    #      改用移动止损退出。此处只把计划止盈与开仓 ATR 记进 ctx（执行层据此不挂固定止盈单）；
    #      TM 的计划止盈仍照常过下面 §4 的 RR 等全部硬护栏校验，一项不改。加仓腿/keep_pending 不在范围。
    if a in ("open", "pending", "replace_pending") and not (a != "open" and pos):
        from desk import demo_exec as _DX
        if (_DX.ecfg(cfg)["trailing_stop"].get("enabled") and tr.get("enabled") and tr.get("active")
                and not tr.get("downgrade") and call.side and call.side == tr.get("side")
                and _num(call.take_profit) and float(mk.get("atr14") or 0) > 0):
            ctx["_trailing_exit"] = {"plan_tp": float(call.take_profit), "atr14": float(mk["atr14"])}
    # 3.7) 周线方向过滤（决策 2，2026-11-15 用户批准，desk/regime_judge.weekly_block_reason）：周线方向是
    #      纯代码数据（完整周收盘 vs 周 MA20 位置 + 五周斜率），与 LLM 判定是否生效无关——判定失败但
    #      特征表在，过滤照常生效。作用域同 §3.5：新入场 open/pending/replace_pending；keep_pending
    #      沿用原单、add 是已有持仓的加仓腿，均不在此列。周线中性/缺失/过滤关闭 → 不拦（fail-soft）。
    if a in ("open", "pending", "replace_pending"):
        from desk import regime_judge as _RJ
        _wb = _RJ.weekly_block_reason(ctx.get("trend"), call.side)
        if _wb:
            errs.append(_wb)
            return errs
    # 3.8) 4H 入场结构（2026-11-15 用户批准，desk/regime_judge.h4_block_reason）：4H 波段是纯代码数据
    #      （已收盘 4H K 线的最近确认枢轴高/低点），与 LLM 判定是否生效无关——判定失败但特征表在，
    #      结构门照常生效。作用域同 §3.7：新入场 open/pending/replace_pending（open 以现价为入场价
    #      比对）；add 加仓腿、keep_pending 沿用原单不在此列。波段缺失/开关关闭 → 不拦（fail-soft）。
    if a in ("open", "pending", "replace_pending"):
        from desk import regime_judge as _RJ
        _px = float(mk["last"]) if a == "open" else _num(call.trigger_px)
        _hb = _RJ.h4_block_reason(ctx.get("trend"), call.side, _px)
        if _hb:
            errs.append(_hb)
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
    if ctx.get("_trailing_exit"):   # 移动止损退出（validate §3.6 已判定资格）：执行层据此不挂固定止盈
        dec._trailing_exit = dict(ctx["_trailing_exit"])
    return dec


def instructions(ctx: dict, cfg=None) -> str:
    ex, pos = ctx.get("existing"), ctx.get("pos")
    lines = ["", "## Order tool (the tool parameters govern; the code does not parse your rationale text)",
             "After the discussion, call the order tool once with the fixed OrderToolCall parameters. The code checks only required fields and hard guardrails; "
             "it never changes your levels, size, or keep/re-hang decision. On failure it returns reasons and allowed ranges: fix and call again on the spot "
             f"(max {TOOL_MAX_ATTEMPTS} attempts; still failing = no conclusion, no order).",
             f"Weighted-consensus direction: {ctx.get('consensus_side') or 'none'} (you cannot flip it; order in this direction or veto).",
             "Required: open/add -> side, entry, stop_loss, take_profit; pending/replace_pending -> side, trigger_px, condition, stop_loss, "
             "take_profit, expire_hours; keep_pending -> side, trigger_px (the existing order's trigger), stop_loss, take_profit, expire_hours; "
             "reduce -> reduce_fraction; every action needs invalidation and rationale.",
             "margin_pct optional: if given, size follows it (hard-guardrail caps only; excess rejected with the cap stated); if omitted, size = per-trade risk x risk_scale.",
             "expire_hours counts from placement: pending/replace_pending from this new placement; keep_pending from the existing order's ORIGINAL placement time (not from now). "
             "To keep the original expiry, set keep_expiry=true on keep_pending/replace_pending (expire_hours then ignored).",
             "keep_pending keeps the original size: size fields (risk_scale, margin_pct) have no effect; do not fill them (filled values are ignored and noted in the receipt).",
             "Intent written in reason/rationale is never executed; actions must go through the action field (e.g. reduce, or changing new_tp). "
             "No to-dos like 'partial take-profit (handle separately)' or 'adjust later' in the rationale: there is no follow-up execution mechanism.",
             "A passed pre-check executes immediately, no confirmation back to you; a rejection returns this call's contracts, notional, margin, stop-loss amount and % of equity with the reasons."]
    lines += baseline_lines(cfg, ctx)
    if ex:
        lines.append(f"Existing conditional order for this coin: {ex.get('algoId')} {ex.get('side')} trigger {ex.get('trigger_px')} stop {ex.get('stop_loss')} "
                     f"take-profit {ex.get('take_profit')} {ex.get('sz')} contracts, placed {ex.get('created') or 'n/a'}, expires {ex.get('expires')} "
                     f"(keep_pending's expire_hours counts from the original placement). You must explicitly choose keep_pending / replace_pending / cancel_pending (or close/reduce).")
    if pos:
        lines.append(f"Existing position in this coin: {pos.get('side')} avg {pos.get('avg_px')} stop {pos.get('sl_px')} take-profit {pos.get('tp_px')}; "
                     "the stop only tightens; same-direction adds use add (market) or pending (conditional add).")
    tr = ctx.get("trend") or {}
    if tr.get("enabled"):
        _na = float((tr.get("cfg") or {}).get("near_atr", 0.3))
        lines.append(f"In a trend session (per the 'Regime judgment (LLM)' section), follow the trend execution rules: open or a near-price pending; near-price means trigger within <={_na:g}xATR14 of spot.")
        lines.append(f"In a trend session, a pending trigger farther than {_na:g}xATR14 from spot will be rejected with the allowed range: move it closer and retry; do not switch to hold to bypass.")
        if tr.get("active"):
            _j = tr.get("judge") or {}
            _atr, _last = float(ctx["mk"].get("atr14") or 0), float(ctx["mk"]["last"])
            _cap = _na * _atr
            lines.append(f"Regime judgment for this coin (LLM): {'short' if tr.get('side') == 'short' else 'long'} trend active "
                         f"(strength {_j.get('strength')}, stage {_j.get('stage')}), late-exhaustion downgrade={'yes' if tr.get('downgrade') else 'no'}; spot {_last:g}, "
                         f"allowed near-price trigger range {_last - _cap:.6g}~{_last + _cap:.6g}.")
        elif tr.get("failed"):
            lines.append("Regime judgment failed for this coin: trend rules NOT active; use the regular rules.")
        _wk = tr.get("weekly") or {}
        if _wk.get("enabled") and _wk.get("direction") in ("long", "short"):
            lines.append(f"Weekly direction filter (code, from completed weekly closes): weekly direction is {_wk['direction']}; "
                         "open/pending/replace_pending against it will be rejected ([WEEKLY_DIR]); "
                         "if consensus opposes the weekly direction, hold and say 'weekly filter' in the rationale; do not try to order.")
        _h4 = tr.get("h4") or {}
        if _h4.get("enabled"):
            _parts = []
            if _h4.get("swing_high") is not None:
                _parts.append(f"short triggers must be >= latest confirmed 4H swing high {float(_h4['swing_high']):g}")
            if _h4.get("swing_low") is not None:
                _parts.append(f"long triggers must be <= latest confirmed 4H swing low {float(_h4['swing_low']):g}")
            if _parts:
                lines.append("4H entry structure (code, closed 4H bars): " + "; ".join(_parts) +
                             "; non-conforming open/pending/replace_pending will be rejected ([H4_STRUCT]); "
                             "a trigger exactly at the swing level passes. Market opens are checked against the same constraint at spot.")
    if cfg is not None and tr.get("enabled"):
        try:
            from desk import demo_exec as _DX
            if _DX.ecfg(cfg)["trailing_stop"].get("enabled"):
                lines.append("Exit mode (code-decided, not yours to choose): when this session's regime judgment is active and the stage is not late_exhaustion, "
                             "your NEW position (a fresh open while this coin has NO position) exits by trailing stop, no fixed take-profit order; "
                             "your take_profit serves only the RR check and the trailing activation line, so still give levels at RR>=1.5 as usual; "
                             "late_exhaustion or no trend keeps the fixed take-profit. "
                             "Conditional ADD orders placed while this coin has a position do NOT use the trailing stop: fixed take-profit, really on the exchange; "
                             "never describe an add-on order as trailing-stop in your rationale.")
        except Exception:
            pass
    return "\n".join(lines)


def baseline_lines(cfg, ctx) -> list[str]:
    """按风险定仓的心算口径 + 本币当前基准张数示例（risk_scale=1/0.5，止损距离 2%/4%，含硬上限削减）。取不到数据只给公式。"""
    out = ["Sizing basis: contracts come from risk_scale x the 1.5% per-trade risk base — do the mental math on this. Base risk amount = equity x 1.5% x consensus-strength scaling x drawdown scaling; "
           "notional = risk amount x risk_scale / (stop distance % + 0.2% cost buffer); contracts = notional / entry price / contract face value; then clipped by hard caps (per-trade margin, in-flight risk, etc.)."]
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
                q = f"{X.contracts(inst, n / px)} contracts" if demo else f"{n / px:.6g} coins"
                ex.append(f"stop distance {d:.0%}, risk_scale {rs:g} -> {q} (notional {n:,.0f} USDT, stop loss approx {n * d:,.0f} USDT)")
                if rs == 1.0:
                    base = r.get("risk_usd")
        if ex:
            out.append(f"Current baseline for this coin (entry {px:g}, base risk amount approx {float(base):,.0f} USDT): " + "; ".join(ex) + ".")
    except Exception:
        pass
    return out


INTENT_KEYWORDS = ("部分止盈", "另行处理", "回头再", "稍后再", "之后再", "择机", "考虑减仓", "考虑加仓", "考虑止盈")


def untriggered_intents(text: str | None) -> list[str]:
    """理由文字里疑似未落地的操作意图（简单关键词，只做提示，不做任何动作）。"""
    t = text or ""
    return [k for k in INTENT_KEYWORDS if k in t]


KEEP_IGNORED_NOTE = "保留挂单不改张数，已忽略"
SIZE_FIELDS = {"margin_pct": None, "risk_scale": 1.0}


def keep_ignored_fields(call: OrderToolCall) -> dict:
    """keep_pending 时带了张数字段（非默认值）→ 返回 {字段: 原值}；不拒绝，只在记录里忽略。"""
    if call.action != "keep_pending":
        return {}
    out = {}
    for k, dflt in SIZE_FIELDS.items():
        v = getattr(call, k)
        if v is not None and (dflt is None or float(v) != float(dflt)):
            out[k] = v
    return out


def strip_keep_size_fields(call: OrderToolCall) -> tuple[OrderToolCall, dict]:
    ign = keep_ignored_fields(call)
    return (call.model_copy(update={k: SIZE_FIELDS[k] for k in ign}) if ign else call), ign


def retry_prompt(prompt: str, history: list) -> str:
    if not history:
        return prompt
    parts = [prompt, "", "## Your previous tool call was rejected — fix the parameters and call again"]
    for i, h in enumerate(history, 1):
        parts.append(f"Attempt {i} parameters: {json.dumps(h['call'], ensure_ascii=False) if h.get('call') else '(unparseable)'}")
        parts.append("Rejection reasons: " + "; ".join(h["errors"]))
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
            call, ign = strip_keep_size_fields(call)
            rc = (ctx.get("_sized") or {}).get(call.action)
            note = None
            if ign:
                note = f"{KEEP_IGNORED_NOTE}（{'、'.join(f'{k}={v:g}' for k, v in ign.items())}）"
                rc = {**(rc or {}), "note": note}
            return {"dec": to_decision(call, ctx), "call": call.model_dump(), "attempts": n, "history": history, "absent_why": None,
                    "receipt": rc, "ignored": ign or None, "ignored_note": note}
        history.append({"call": call.model_dump(), "errors": errs})
    last = "；".join(history[-1]["errors"]) if history else "无输出"
    return {"dec": None, "call": history[-1]["call"] if history else None, "attempts": TOOL_MAX_ATTEMPTS, "history": history,
            "absent_why": f"交易经理下单工具 {TOOL_MAX_ATTEMPTS} 次调用均未通过校验（最后原因：{last[:300]}）"}
