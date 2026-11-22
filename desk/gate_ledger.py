"""反事实门账（2026-11-22 审查共识包 B 项；口径经两轮审查定版，裁决规则已预注册于
根目录 constraints.yaml 的 gate_adjudication 段——先写规则、后收数据）。

做什么：每个决策落盘后，把交易经理的**原始提案**（下单工具第一次尝试的参数）拿出来，
逐道门单独记账：这道门当时是 would_block / pass / clamp / advisory / na，拦没拦住（bind），
提案几何（RR、止损距离、名义、风险金额）一律调生产同一套 validate / sizing / receipt 函数
在**上下文副本**上重算，不手写第二套几何。到期后按「1H 触价、同根 K 止损优先」算反事实 R，
与实际执行方案的 R 对照，得出每道门的 gate_value_r。四格全记（拦对/拦错/放行赚/放行亏），
不只记拦截——只记拦截会系统性抬高每道门的精确率。

铁律（记录层定位）：
- fail-soft 旁观者：本模块任何异常只记 demo_events（gate_ledger_failed），绝不拦截、降速
  或改变任何决策；record_from_decision 在 manager 里也是 try/except 包裹的最后一环。
- 幂等：门账以 decision_id 为键、评分以 (decision_id, gate) 为键，重复运行不双记
  （照 demo_ws_seen 的防重入口径）。
- 版本快照：每行记 config_hash + judge_std + 当日门参数实值（near_atr / oversold_rs_cap /
  weekly_mode / h4 开关）+ tiers（读 constraints.yaml，读不到用内置副本）——没有版本快照，
  攒下的样本会跨着几套规则没法裁决。
- 判定 raw/effective 双记：judge.stage_raw（模型原值）与 stage（代码门后的生效值）都入账，
  末段降级的反事实才对得上口径。
- 评分只引用 scores.jsonl 的记录键（scoring_source），不建第二套真相。
"""
from __future__ import annotations

import copy
import datetime as _dt
import math

from desk import settings

LEDGER = settings.STATE / "ledger" / "gate_ledger.jsonl"
REVIEWS = settings.STATE / "ledger" / "gate_reviews.jsonl"

GATES = ("DIRECTION", "TREND_DIST", "H4_STRUCT", "TREND_OVERSOLD", "WEEKLY_DIR")
_FALLBACK_TIERS = {"DIRECTION": "soft", "TREND_DIST": "soft", "H4_STRUCT": "soft",
                   "TREND_OVERSOLD": "soft", "WEEKLY_DIR": "advisory"}
ORDER_ACTS = ("open", "add", "pending", "replace_pending", "keep_pending")
_GEOM_ERR_TAGS = ("[STOP_SIDE]", "[STOP_MAX]", "[TP_SIDE]", "[RR]")
_REGIME_EN = {"上涨": "up", "下跌": "down", "震荡": "range"}


def HOURLY(inst):   # 可在测试中替换（与 scoring.HOURLY 同口径：1H 已收盘 K 线）
    from desk import okx_data
    return okx_data.candles(inst, "1H", 1500)


def _tiers() -> dict:
    try:
        import yaml
        reg = yaml.safe_load((settings.ROOT / "constraints.yaml").read_text(encoding="utf-8"))
        out = {c["id"]: c["tier"] for c in reg.get("constraints", []) if c.get("id") in GATES}
        return {**_FALLBACK_TIERS, **out}
    except Exception:
        return dict(_FALLBACK_TIERS)


def _event(kind: str, **kw):
    try:
        from desk import okx_demo
        okx_demo.log_event(kind, **kw)
    except Exception:
        pass


def _num(v):
    try:
        return float(v) if v not in (None, "") and float(v) > 0 else None
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ 决策时记账

def _first_proposal(tool_res) -> tuple[dict | None, int]:
    """交易经理的原始提案 = 下单工具第一次尝试的参数（被拒后改的参数是「改道」，不算提案）。
    返回 (call_dict, attempt_no)；解析失败的空尝试跳过。"""
    if not tool_res:
        return None, 0
    hist = tool_res.get("history") or []
    for i, h in enumerate(hist, 1):
        if h.get("call"):
            return h["call"], i
    if tool_res.get("call"):
        return tool_res["call"], len(hist) + 1
    return None, 0


def _geometry(cfg, call, ctx, errs) -> dict:
    """提案几何：valid_geometry 看生产 validate 的 §4 错误标记；rr/止损距离用 sizing 生产函数；
    名义/风险金额优先取 validate 在副本上算出的 _sized 回执（生产 _qty 路径），取不到再试 _qty。"""
    from desk import sizing
    from desk import tm_order_tool as T
    a = call.action
    px = float(ctx["mk"]["last"]) if a in ("open", "add") else _num(call.trigger_px)
    sl, tp = _num(call.stop_loss), _num(call.take_profit)
    out = {"rr": None, "stop_dist_pct": None, "notional": None, "risk_usd": None,
           "valid_geometry": not any(t in e for e in errs for t in _GEOM_ERR_TAGS)}
    if px and sl:
        out["stop_dist_pct"] = round(abs(px - sl) / px, 6)
        rr = sizing.rr_ratio(call.side, px, sl, tp) if tp else None
        out["rr"] = round(rr, 4) if rr is not None else None
    rc = (ctx.get("_sized") or {}).get(a)
    if rc:
        out["notional"], out["risk_usd"] = rc.get("notional"), rc.get("stop_loss_usd")
    elif px and sl:
        try:
            q = T._qty(cfg, call, ctx, px, sl)
            if not isinstance(q, str):
                qty, sz, lev, how = q
                r = T.receipt(ctx["bk"], sz, qty, px, sl, lev, how)
                out["notional"], out["risk_usd"] = r.get("notional"), r.get("stop_loss_usd")
        except Exception:
            pass
    return out


def evaluate_gates(cfg, call, ctx) -> tuple[list[dict], list[str], dict]:
    """逐门评估（ctx 必须是副本）：先在副本上跑一遍生产 validate 拿到错误集与留痕，
    再逐门用生产子函数独立判定（validate 会早退，单跑一遍看不全每道门的状态）。"""
    from desk import regime_judge as RJ
    from desk import tm_order_tool as T
    errs = T.validate(cfg, call, ctx)
    notes = list(ctx.get("_gate_notes") or [])
    tr = ctx.get("trend") or {}
    mk = ctx["mk"]
    a, side = call.action, call.side
    gates: list[dict] = []

    # DIRECTION（validate §3）
    if any(e.startswith("[DIRECTION]") for e in errs):
        gates.append({"id": "DIRECTION", "state": "would_block", "bind": True})
    else:
        gates.append({"id": "DIRECTION", "state": "pass", "bind": False})

    trend_on = bool(tr.get("enabled"))
    active = bool(tr.get("active")) and side == tr.get("side")
    tc = tr.get("cfg") or {}

    # TREND_DIST（§3.5 近价带）：带宽取生产配置值与 mk 的 ATR（与 validate 同源输入）
    if not trend_on:
        gates.append({"id": "TREND_DIST", "state": "na", "bind": False})
    elif not (active and a in ("pending", "replace_pending") and _num(call.trigger_px)
              and float(mk.get("atr14") or 0) > 0):
        gates.append({"id": "TREND_DIST", "state": "na", "bind": False})
    else:
        atr, last = float(mk.get("atr14") or 0), float(mk["last"])
        cap = float(tc.get("near_atr", 0.3)) * atr
        trig = float(call.trigger_px)
        lo_b, hi_b = last - cap, last + cap
        if trig < lo_b - 1e-9 or trig > hi_b + 1e-9:
            yielded = any("TREND_DIST" in n and "H4_STRUCT" in n for n in notes)
            gates.append({"id": "TREND_DIST", "state": "would_block",
                          "bind": any(e.startswith("[TREND_DIST]") for e in errs),
                          "bound": {"lo": round(lo_b, 6), "hi": round(hi_b, 6)}, "probe": trig,
                          "excess_atr": round((abs(trig - last) - cap) / atr, 4) if atr > 0 else None,
                          **({"yielded_to": "H4_STRUCT"} if yielded else {})})
        else:
            gates.append({"id": "TREND_DIST", "state": "pass", "bind": False,
                          "bound": {"lo": round(lo_b, 6), "hi": round(hi_b, 6)}, "probe": trig})

    # H4_STRUCT（§3.8）：生产 h4_block_reason + validate 同款突破保底裁定
    if not trend_on:
        gates.append({"id": "H4_STRUCT", "state": "na", "bind": False})
    elif a not in ("open", "pending", "replace_pending"):
        gates.append({"id": "H4_STRUCT", "state": "na", "bind": False})
    else:
        px = float(mk["last"]) if a == "open" else _num(call.trigger_px)
        reason = RJ.h4_block_reason(tr, side, px)
        if not reason:
            gates.append({"id": "H4_STRUCT", "state": "pass", "bind": False})
        else:
            feat = tr.get("features") or {}
            key = "lo_20" if side == "short" else "hi_20" if side == "long" else None
            ext = mk.get(key) if key else None
            if ext is None:
                ext = feat.get(key) if key else None
            last = float(mk["last"])
            brk = (isinstance(ext, (int, float)) and math.isfinite(float(ext))
                   and ((last <= float(ext)) if side == "short" else (last >= float(ext))))
            if brk:
                gates.append({"id": "H4_STRUCT", "state": "pass", "bind": False, "note": "breakout_pass"})
            else:
                gates.append({"id": "H4_STRUCT", "state": "would_block", "bind": True})

    # TREND_OVERSOLD（§3.5 降级）：open 禁令 = would_block；rs 超限 = clamp（2026-11-22 起）
    # 作用域与 validate §3.5 严格一致：open/pending/replace_pending（add/keep 不在降级作用域）
    if not trend_on or not active or not tr.get("downgrade") or a not in ("open", "pending", "replace_pending"):
        gates.append({"id": "TREND_OVERSOLD", "state": "na", "bind": False})
    elif a == "open":
        gates.append({"id": "TREND_OVERSOLD", "state": "would_block", "bind": True, "note": "open_banned"})
    else:
        cap = float(tc.get("oversold_risk_scale", 0.5))
        rs = float(call.risk_scale if call.risk_scale is not None else 1.0)
        if rs > cap + 1e-12:
            gates.append({"id": "TREND_OVERSOLD", "state": "clamp", "bind": True, "from": rs, "to": cap})
        else:
            gates.append({"id": "TREND_OVERSOLD", "state": "pass", "bind": False})

    # WEEKLY_DIR（§3.7）：生产 weekly_block_reason；advisory 模式记 advisory 不算拦
    wk = tr.get("weekly") or {}
    if not wk.get("enabled"):
        gates.append({"id": "WEEKLY_DIR", "state": "na", "bind": False})
    elif a not in ("open", "pending", "replace_pending"):
        gates.append({"id": "WEEKLY_DIR", "state": "na", "bind": False})
    else:
        reason = RJ.weekly_block_reason(tr, side)
        if not reason:
            gates.append({"id": "WEEKLY_DIR", "state": "pass", "bind": False})
        elif wk.get("mode", "advisory") == "block":
            gates.append({"id": "WEEKLY_DIR", "state": "would_block", "bind": True})
        else:
            gates.append({"id": "WEEKLY_DIR", "state": "advisory", "bind": False})
    return gates, errs, _geometry(cfg, call, ctx, errs)


def record_from_decision(cfg, rec: dict, tctx: dict, tool_res, regime) -> dict | None:
    """manager.decide_and_execute 落盘决策后调用（旁观者）：组装一行门账并追加。
    无下单类提案（hold/缺席/解析全失败）不记（否决另有评分体系）。任何异常只记事件、返回 None。"""
    try:
        return _record(cfg, rec, tctx, tool_res, regime)
    except Exception as e:
        _event("gate_ledger_failed", decision_id=rec.get("decision_id"),
               error=f"{type(e).__name__}: {str(e)[:200]}")
        return None


def _record(cfg, rec, tctx, tool_res, regime) -> dict | None:
    from desk import tm_order_tool as T
    did = rec.get("decision_id")
    if not did:
        return None
    if any(r.get("decision_id") == did for r in settings.read_jsonl(LEDGER)):
        return None   # 幂等：同决策不双记（续跑/重入）
    prop_d, attempt = _first_proposal(tool_res)
    if not prop_d:
        return None
    call = T.OrderToolCall(**prop_d)
    if call.action not in ORDER_ACTS:
        return None
    ctx = copy.deepcopy(tctx)
    gates, _errs, geom = evaluate_gates(cfg, call, ctx)

    regime = regime or {}
    judge = regime.get("judge") or {}
    tc = regime.get("cfg") or {}
    wk = regime.get("weekly") or {}
    h4 = regime.get("h4") or {}
    final_d = (tool_res or {}).get("call") or {}
    execd = rec.get("execution") or {}
    executed = bool(execd.get("ok")) and final_d.get("action") in ORDER_ACTS
    same = executed and all(final_d.get(k) == prop_d.get(k)
                            for k in ("action", "side", "trigger_px", "entry", "stop_loss", "take_profit"))
    actual = {"executed": executed,
              "act": final_d.get("action") if executed else None,
              "trig": final_d.get("trigger_px"), "entry": final_d.get("entry"),
              "sl": final_d.get("stop_loss"), "tp": final_d.get("take_profit"),
              "condition": final_d.get("condition"),
              "rs": final_d.get("risk_scale"),
              "note": "as_proposed" if same else ("rerouted" if executed else "not_executed")}
    from desk import scoring
    regime_label = _REGIME_EN.get(scoring.regime_of(float((rec.get("market") or {}).get("ret_20d") or 0), cfg), "range")
    row = {"v": 1, "decision_id": did, "ts": rec.get("time"), "date": rec.get("date"),
           "session": rec.get("session"), "source": rec.get("source"), "inst": rec.get("inst_id"),
           "cluster_day": rec.get("date"), "config_hash": rec.get("config_hash"),
           "judge_std": regime.get("judge_standard"),
           "judge": {"dir": judge.get("direction"), "strength": judge.get("strength"),
                     "stage_raw": judge.get("stage_raw", judge.get("stage")),
                     "stage_effective": judge.get("stage"), "conf": judge.get("confidence"),
                     "failed": bool(regime.get("failed"))},
           "regime_label": regime_label,
           "gate_cfg": {"trend_enabled": bool(regime.get("enabled")),
                        "near_atr": tc.get("near_atr"), "oversold_rs_cap": tc.get("oversold_risk_scale"),
                        "weekly_mode": wk.get("mode"), "weekly_enabled": bool(wk.get("enabled")),
                        "h4_enabled": bool(h4.get("enabled"))},
           "tiers": _tiers(),
           "proposal": {"act": call.action, "side": call.side, "trig": call.trigger_px,
                        "entry": call.entry, "spot": float((tctx.get("mk") or {}).get("last") or 0) or None,
                        "condition": call.condition, "sl": call.stop_loss, "tp": call.take_profit,
                        "rs_pre_clamp": call.risk_scale, "horizon_days": call.horizon_days,
                        "attempt": attempt, **geom},
           "gates": gates, "actual": actual, "outcome": {"status": "pending"}}
    settings.append_jsonl(LEDGER, row)
    return row


# ------------------------------------------------------------------ 到期评分（反事实 R）

def _t0_utc(ts: str) -> _dt.datetime:
    """决策时刻（北京时区字符串）→ naive UTC，向上取整到整点（只用决策之后开盘的 1H K 线，与 scoring 同口径）。"""
    t = _dt.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S%z").astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return _ceil_hour(t)


def _ceil_hour(t: _dt.datetime) -> _dt.datetime:
    base = t.replace(minute=0, second=0, microsecond=0)
    return base if base == t else base + _dt.timedelta(hours=1)


def _path_r(bars, entry, sl, tp, sgn) -> float | None:
    """bars = 入场后（含入场根）的 (high, low, close) 序列：同根先判止损（保守）。
    返回 R：止盈先触 +RR、止损先触 −1、都没触 = 窗口末收盘的浮动 R。"""
    risk = abs(entry - sl)
    if risk <= 0 or not bars:
        return None
    rr = abs(tp - entry) / risk if tp else None
    for hi, lo, _cl in bars:
        if (lo <= sl) if sgn > 0 else (hi >= sl):
            return -1.0
        if tp and ((hi >= tp) if sgn > 0 else (lo <= tp)):
            return round(rr, 3)
    return round((bars[-1][2] - entry) * sgn / risk, 3)


def _cf_eval(df, t0, horizon_h, *, spot, trig, condition, sl, tp, side):
    """反事实路径评估：pending 先等触发（窗口内未触发 → (None, False) 表示不可成交）；
    open 以决策现价入场。返回 (pnl_r, tradeable)。df 为 1H 线（Date 为 naive UTC）。"""
    import pandas as pd
    sgn = 1 if side == "long" else -1
    end = t0 + _dt.timedelta(hours=horizon_h)
    win = df[(df.Date >= pd.Timestamp(t0)) & (df.Date < pd.Timestamp(end))]
    if win.empty:
        return None, False
    bars = [(float(r.High), float(r.Low), float(r.Close)) for r in win.itertuples()]
    if trig:
        idx = None
        for i, (hi, lo, _c) in enumerate(bars):
            if (hi >= trig) if condition == "gte" else (lo <= trig):
                idx = i
                break
        if idx is None:
            return None, False
        return _path_r(bars[idx:], float(trig), sl, tp, sgn), True
    return _path_r(bars, float(spot), sl, tp, sgn), True


def review_due(cfg, now=None) -> list[dict]:
    """到期门账行逐门评分，追加 gate_reviews.jsonl（幂等：(decision_id, gate) 已评不再评）。
    只落盘、不裁决；裁决口径见 constraints.yaml 预注册段。异常逐行隔离，整体失败由调用方记 errors。"""
    now = now or settings.now()
    now_naive = now.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    done = {(r.get("decision_id"), r.get("gate")) for r in settings.read_jsonl(REVIEWS)}
    score_ids = {s.get("id") for s in settings.read_jsonl(settings.STATE / "scores.jsonl")}
    out, cache = [], {}
    for row in settings.read_jsonl(LEDGER):
        did = row.get("decision_id")
        prop = row.get("proposal") or {}
        hz = int(prop.get("horizon_days") or 5)
        hz = min(max(hz, 1), 30)
        try:
            t0 = _t0_utc(row["ts"])
        except Exception:
            continue
        if now_naive < t0 + _dt.timedelta(hours=24 * hz):
            continue   # 未到期
        inst = row.get("inst")
        if inst not in cache:
            try:
                cache[inst] = HOURLY(inst)
            except Exception:
                cache[inst] = None
        df = cache[inst]
        for g in row.get("gates") or []:
            gid = g.get("id")
            if (did, gid) in done or g.get("state") in ("na",):
                continue
            rev = _review_one(cfg, row, g, df, t0, hz, score_ids)
            if rev is not None:
                settings.append_jsonl(REVIEWS, rev)
                done.add((did, gid))
                out.append(rev)
    return out


def _review_one(cfg, row, g, df, t0, hz, score_ids) -> dict | None:
    did, gid = row.get("decision_id"), g.get("id")
    prop = row.get("proposal") or {}
    side = prop.get("side")
    base = {"v": 1, "decision_id": did, "gate": gid, "judged_at": settings.now_str(),
            "scoring_source": (f"scores.jsonl#{did}:trade_manager" if f"{did}:trade_manager" in score_ids else None),
            "fill_model": "1H 触价, 同根 K 止损优先",
            "cell": {"regime": row.get("regime_label"), "inst": (row.get("inst") or "").split("-")[0]},
            "cluster_day": row.get("cluster_day")}
    if df is None or not prop.get("valid_geometry") or not prop.get("sl"):
        return {**base, "tradeable": False, "cf_pnl_r": None, "cf_verdict": "untradeable",
                "actual_alt_pnl_r": None, "gate_value_r": None,
                "reason": "data_missing" if df is None else "invalid_geometry"}
    cf_r, tradeable = _cf_eval(df, t0, 24 * hz, spot=prop.get("spot"), trig=prop.get("trig"),
                               condition=prop.get("condition"), sl=float(prop["sl"]),
                               tp=prop.get("tp"), side=side)
    if not tradeable:
        return {**base, "tradeable": False, "cf_pnl_r": None, "cf_verdict": "untradeable",
                "actual_alt_pnl_r": None, "gate_value_r": None, "reason": "not_triggered"}
    act = row.get("actual") or {}
    alt_r = 0.0
    if act.get("executed") and act.get("sl"):
        a_r, a_tr = _cf_eval(df, t0, 24 * hz, spot=prop.get("spot"), trig=act.get("trig"),
                             condition=act.get("condition"), sl=float(act["sl"]),
                             tp=act.get("tp"), side=side)
        alt_r = a_r if (a_tr and a_r is not None) else 0.0
    state = g.get("state")
    if state == "would_block":
        verdict = "block_correct" if cf_r < 0 else ("block_wrong" if cf_r > 0 else "block_neutral")
        value = round(alt_r - cf_r, 3) if g.get("bind") else 0.0
    elif state == "clamp":
        verdict, value = "clamped", 0.0   # R 口径下 clamp 只降名义不改单位风险收益，价值记 0 并注明
    elif state == "advisory":
        verdict, value = ("advisory_would_block_correct" if cf_r < 0 else "advisory_would_block_wrong"), 0.0
    else:   # pass
        verdict = "pass_good" if cf_r > 0 else ("pass_bad" if cf_r < 0 else "pass_neutral")
        value = 0.0
    return {**base, "tradeable": True, "cf_pnl_r": cf_r, "cf_verdict": verdict,
            "actual_alt_pnl_r": alt_r, "gate_value_r": value,
            **({"note": "clamp 在 R 口径下价值为 0（只降名义）"} if state == "clamp" else {})}


if __name__ == "__main__":
    cfg = settings.load_config()
    rows = review_due(cfg)
    print(f"gate_ledger: 本次新评 {len(rows)} 行")
