"""分析→执行价格漂移检查（2026-10-28）。

分析开始时记一次参考价 analysis_px（公共行情 last），执行前对「新开仓（现价/条件单）和同向加仓」再取一次最新公共行情价，
|偏离| 超过 drift_check.max_pct（%）则跳过这一单：记事件 drift_skipped、决策记录 drift_skipped、日报写原因。
持有/平仓/减仓/止损止盈调整/撤单不经过这里。取现价失败不阻断（记 drift_check_price_unavailable 告警事件）。
与 risk.max_price_drift_atr（pre_trade_gate，按 ATR 比较 market_ctx 与执行时价格）是两道独立检查。"""
from desk import okx_data, settings

DEFAULTS = {"enabled": True, "max_pct": 1.0}


def dcfg(cfg) -> dict:
    return {**DEFAULTS, **((cfg or {}).get("drift_check") or {})}


def stamp(inst: str) -> dict | None:
    """分析开始时的参考价；取不到返回 None（之后该币不做漂移检查，并记告警事件）。"""
    try:
        return {"px": float(okx_data.last_price(inst)), "time": settings.now_str()}
    except Exception as e:
        from desk import okx_demo
        okx_demo.log_event("drift_ref_unavailable", inst_id=inst, error=f"{type(e).__name__}: {e}"[:200])
        return None


def skip(cfg, inst, did, mk, agg, notes, kind: str) -> bool:
    """kind：open / add。返回 True 表示本单因漂移跳过（已记事件与 notes）。"""
    c = dcfg(cfg)
    ref = (mk or {}).get("_analysis_ref") or {}
    if not c["enabled"] or not ref.get("px"):
        return False
    from desk import okx_demo
    lim = float(c["max_pct"])
    try:
        now_px = float(okx_data.last_price(inst))
    except Exception as e:
        err = f"{type(e).__name__}: {e}"[:200]
        okx_demo.log_event("drift_check_price_unavailable", inst_id=inst, decision_id=did, leg=kind,
                           analysis_px=ref["px"], error=err)
        agg["drift_check"] = {"ok": None, "leg": kind, "analysis_px": ref["px"], "error": err}
        notes.append(f"⚠️ 漂移检查取现价失败（{type(e).__name__}），不阻断，按原流程执行")
        return False
    dev = (now_px - float(ref["px"])) / float(ref["px"]) * 100
    info = {"leg": kind, "analysis_px": ref["px"], "analysis_px_time": ref.get("time"), "now_px": now_px,
            "now_time": settings.now_str(), "deviation_pct": round(dev, 4), "max_pct": lim}
    agg["drift_check"] = {"ok": abs(dev) <= lim, **info}
    if abs(dev) <= lim:
        return False
    agg["drift_skipped"] = info
    okx_demo.log_event("drift_skipped", inst_id=inst, decision_id=did, **info)
    notes.append(f"价格漂移跳过{'加仓' if kind == 'add' else '开仓'}：分析时 {ref['px']:.6g} → 现价 {now_px:.6g}，"
                 f"偏离 {dev:+.2f}% 超过阈值 ±{lim:g}%")
    return True


def describe(d: dict) -> str:
    return (f"**价格漂移跳过{'加仓' if d.get('leg') == 'add' else '开仓'}**：分析时价格 {d['analysis_px']:.6g}"
            f"（{str(d.get('analysis_px_time') or '')[11:16] or '—'}）→ 执行时 {d['now_px']:.6g}"
            f"（{str(d.get('now_time') or '')[11:16] or '—'}），偏离 {d['deviation_pct']:+.2f}%，超过阈值 ±{d['max_pct']:g}%，本单不下")
