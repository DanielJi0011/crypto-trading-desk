"""条件入场单「触发后限价」的超时处理（execution.entry_limit）。

!! 禁止实盘：所有调用都经 desk/okx_demo.py（OKX_DEMO_* + x-simulated-trading: 1）。只处理本模块登记过的条件单
!! （place_trigger 在 entry_limit.enabled 时登记），不碰其它任何现有订单。

超时语义由 execution.entry_limit.fallback_market 决定：
  false（默认，2026-11-11 用户决定「这个兜底就没有必要了，就等它触发」）：取消市价兜底。
    超时撤子单后绝不市价追单——过护栏后看现价与触发价的相对位置：价格回到等待侧
    （gte 单现价<触发价 / lte 单现价>触发价，即触发条件当前不成立）→ 以原触发价/止损止盈/
    剩余张数重挂一张条件单（沿用原到期 expires 与原 decision 关联，可多轮重挂、始终受原到期约束）；
    价格已穿过触发价且未回（条件当前成立）→ 本次入场作罢（entry_missed_no_chase），等下一场重新决策。
    已成交部分照常重挂保护。市价兜底事故背景：2026-11-11 ETH 计划 2075、市价兜底成交 2055.74、RR 1.76→1.007。
  true（旧行为，回滚用）：撤单后市价补剩余（filling/protecting 状态机），且补单发出前先过兜底前判定
    （desk/fallback_judge.py）：交易经理身份 + 快速模型判断当前价位入场是否仍合理，结论落
    entries[algoId].fallback_judge 后再执行；否决/判定失败 → result=abandoned，开关 fallback_judge_enabled。

状态文件 state/ledger/entry_limit.json：entries[algoId] = {...,status}
  armed      已挂条件单，未触发
  triggered  已触发，记录 ordId（限价子单）与 triggered_ms（触发时间；demo_ws 收到触发事件时先记，否则取算法单历史 triggerTime/uTime）
  cancelling 已决定撤限价子单（先落盘再发请求）
  filling    （仅 fallback_market=true）已生成市价补单 clOrdId（先落盘再下单；重入时按 clOrdId 查单，不会重复补单）
  protecting （仅 fallback_market=true）补单已完成，正在按总仓位重挂止损止盈
  done       终态（filled / fallback_done / blocked / abandoned / rearmed / missed_no_chase / gone 等，见 result）
防重入：fcntl 文件锁 state/ledger/entry_limit.lock（非阻塞，拿不到锁本轮跳过）+ 上述状态机。
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import fcntl
import time

from desk import okx_demo as X
from desk import settings

PATH = settings.STATE / "ledger" / "entry_limit.json"
LOCK = settings.STATE / "ledger" / "entry_limit.lock"


def _load() -> dict:
    s = settings.read_json(PATH, {})
    s.setdefault("entries", {})
    return s


def _save(s):
    settings.write_json(PATH, s)


@contextlib.contextmanager
def _locked():
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    f = open(LOCK, "w")
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False; return
        yield True
    finally:
        f.close()


def _update(aid, **kw):
    s = _load(); e = s["entries"].setdefault(aid, {}); e.update(kw); _save(s); return e


def register(aid, **meta):
    with _locked() as ok:   # 下单路径不等锁也要登记（新 algoId，不会与检查冲突）
        s = _load()
        s["entries"][str(aid)] = {**meta, "algoId": str(aid), "status": "armed", "created": settings.now_str()}
        _save(s)
    X.log_event("entry_limit_registered", algoId=str(aid), inst_id=meta.get("inst"), order_px=meta.get("order_px"), sz=meta.get("sz"))


def record_trigger(aid, ms=None, ord_id=None):
    """demo_ws 收到条件单触发事件时调用：只记触发时间，不下单。未登记的 algoId 忽略。"""
    aid = str(aid)
    s = _load(); e = s["entries"].get(aid)
    if not e or e.get("triggered_ms"):
        return False
    e["triggered_ms"] = int(ms or time.time() * 1000)
    if ord_id:
        e["ordId"] = str(ord_id)
    _save(s)
    X.log_event("entry_limit_trigger_seen", algoId=aid, inst_id=e.get("inst"), triggered_ms=e["triggered_ms"], source="ws")
    return True


# ------------------------------------------------------------------ 工具
def _ct(inst):
    return X.instrument(inst)


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _get_order(inst, ord_id=None, cl=None):
    kw = {"instId": inst, **({"ordId": ord_id} if ord_id else {"clOrdId": cl})}
    try:
        d = X.get("/api/v5/trade/order", **kw).get("data") or []
    except X.DemoError as e:
        if cl and ("51603" in str(e) or "not exist" in str(e).lower()):
            return None
        raise
    return d[0] if d else None


def _guard(cfg, e: dict | None = None) -> list:
    """补单前护栏：KILL/HALT、当日亏损与回撤（R5，实时权益含浮亏，读取失败即拒绝）、已实现日亏（读取失败即拒绝）。
    不查单币一仓（本单就是该币的这笔仓位）。完整 check_open 复用见 R6（第二批）。"""
    from desk import demo_exec as DX, equity_guard
    reasons = []
    if settings.kill_active():
        reasons.append("[KILL] 紧急停止文件存在，禁止补单")
    if settings.halt_active():
        reasons.append("[HALT] 全冻结文件存在，禁止补单")
    reasons += equity_guard.reasons(cfg)
    if e is not None:   # R12：同币另有未完结入场子单（不是本条的子单）→ 视同已有持仓，不补单
        try:
            others = [o for o in DX.pending_entry_orders(e["inst"]) if str(o.get("ordId")) != str(e.get("ordId"))]
            if others:
                reasons.append(f"[ONE_PER_COIN] 同币另有在途入场子单 {[o.get('ordId') for o in others]}，禁止补单")
        except Exception as ex:
            reasons.append(f"[GUARD_DATA] 在途入场子单读取失败（{type(ex).__name__}），禁止补单")
    try:
        pnl = DX.today_realized()
        den = DX.initial_equity() if DX.DEMO_ACCOUNT.exists() else None
        if den and pnl <= -den * cfg["guardrails"]["max_daily_loss_pct"] / 100:
            reasons.append(f"[DAILY_LOSS] 模拟盘当日已实现亏损 {pnl:.2f} 达到上限，禁止补单")
    except Exception as e:
        X.log_event("guard_data_unavailable", what="positions-history", error=type(e).__name__)
        reasons.append(f"[GUARD_DATA] 当日已实现盈亏读取失败（{type(e).__name__}），禁止补单")
    if e is not None and e.get("remain") is not None:
        reasons += _full_check(cfg, e)
    return list(dict.fromkeys(reasons))


def _full_check(cfg, e) -> list:
    """R6：补单前跑完整 check_open。本笔已成交部分与本条子单视为同一笔交易：从视图中剔除本币持仓与本条子单，
    再按（已成交 + 待补）的总张数作为新开仓检查总敞口、同向净名义、在途风险、杠杆、止损等。"""
    from desk import demo_exec as DX
    try:
        bk = DX.book(cfg)
        inst = e["inst"]
        held = next((p for p in bk["positions"] if p["inst_id"] == inst and not p.get("legacy")), None)
        if e.get("add") and held:
            # 条件加仓单：现有持仓（含本单已成交部分）保留在视图里，按加仓模式检查待补的部分
            bk = {**bk, "pending_entries": [p for p in (bk.get("pending_entries") or []) if p["inst_id"] != inst],
                  "triggers": [t for t in bk["triggers"] if t["instId"] != inst]}
            ct = float(_ct(inst)["ctVal"])
            px = float(X.get("/api/v5/market/ticker", instId=inst)["data"][0]["last"])
            rs, _ = DX.check_open(cfg, bk, inst, _num(e.get("remain")) * ct, px, e.get("stop_loss"), e["side"],
                                  float(held.get("leverage") or e.get("lever") or 1), add=held)
            return [str(x) for x in rs]
        bk = {**bk, "positions": [p for p in bk["positions"] if p["inst_id"] != inst],
              "pending_entries": [p for p in (bk.get("pending_entries") or []) if p["inst_id"] != inst],
              "triggers": [t for t in bk["triggers"] if t["instId"] != inst]}
        bk["exposure"] = sum(p.get("notional", 0) for p in bk["positions"])
        ct = float(_ct(inst)["ctVal"])
        px = float(X.get("/api/v5/market/ticker", instId=inst)["data"][0]["last"])
        tot = (_num(e.get("accFillSz")) + _num(e.get("remain"))) * ct
        rs, _ = DX.check_open(cfg, bk, inst, tot, px, e.get("stop_loss"), e["side"], float(e.get("lever") or 1))
        return [str(x) for x in rs]
    except Exception as ex:
        X.log_event("guard_data_unavailable", what="fallback_check_open", error=type(ex).__name__)
        return [f"[GUARD_DATA] 补单完整风控检查失败（{type(ex).__name__}），禁止补单"]


def _close_algos_for(e) -> list:
    """该币该方向现有的止损止盈算法单（conditional/oco，reduceOnly 平仓方向）。"""
    from desk import demo_exec as DX
    close_side = "sell" if e["side"] == "long" else "buy"
    return [a for a in DX.close_algos(e["inst"])
            if a.get("posSide") == e.get("pos_side", "net") and a.get("side") in (None, "", close_side)]


def _position_contracts(e) -> float:
    from desk import demo_exec as DX
    tot = 0.0
    for p in DX.demo_positions():
        if p["instId"] == e["inst"] and p.get("posSide") in (e.get("pos_side", "net"),):
            tot += abs(_num(p["pos"]))
    return tot


def _det(*parts):
    from desk.demo_exec import det_id
    return det_id(*parts)


def _place_oco(e, sz) -> str:
    inst = e["inst"]
    sl, tp = e.get("stop_loss"), e.get("take_profit")
    from desk.demo_exec import det_id
    cl = det_id("elp", e.get("aid") or inst, e.get("pos_side", "net"), sz, sl, tp, settings.now_str()[:16])   # R11.1：同一补保护单重试同号
    body = {"instId": inst, "tdMode": "isolated", "side": "sell" if e["side"] == "long" else "buy",
            "ordType": "oco" if (sl and tp) else "conditional", "sz": sz, "reduceOnly": True, "algoClOrdId": cl}
    if e.get("pos_side", "net") != "net":
        body["posSide"] = e["pos_side"]
    if sl:
        body.update(slTriggerPx=X._px(inst, sl), slOrdPx="-1", slTriggerPxType="last")
    if tp:
        body.update(tpTriggerPx=X._px(inst, tp), tpOrdPx="-1", tpTriggerPxType="last")
    d = X.post("/api/v5/trade/order-algo", body)["data"][0]
    return d.get("algoId")


def _protect_total(aid, e):
    """普通入场：按总仓位重挂一张 OCO（先挂新再撤旧）。
    条件加仓单（e.add）：只为本腿成交张数挂独立保护，不撤任何既有保护单。"""
    if e.get("add"):
        return _protect_add_leg(aid, e)
    total = _position_contracts(e)
    if total <= 0:
        X.log_event("entry_limit_protect_skip", algoId=aid, inst_id=e["inst"], why="无持仓")
        return None
    old = _close_algos_for(e)
    covered = sum(_num(a.get("sz")) for a in old)
    X.log_event("entry_limit_protect_check", algoId=aid, inst_id=e["inst"], position=total,
                existing=[{"algoId": a["algoId"], "sz": a.get("sz"), "algoClOrdId": a.get("algoClOrdId")} for a in old])
    if old and len(old) == 1 and abs(covered - total) < 1e-9:
        X.log_event("entry_limit_protect_ok", algoId=aid, inst_id=e["inst"], sz=covered)
        return old[0]["algoId"]
    new_id = _place_oco({**e, "aid": aid}, X._dec(total))
    X.log_event("entry_limit_oco_placed", algoId=aid, inst_id=e["inst"], new_algoId=new_id, sz=X._dec(total))
    if old:
        X.post("/api/v5/trade/cancel-algos", [{"instId": e["inst"], "algoId": a["algoId"]} for a in old])
        X.log_event("entry_limit_old_sltp_cancelled", algoId=aid, inst_id=e["inst"], cancelled=[a["algoId"] for a in old])
    return new_id


def _protect_add_leg(aid, e):
    """加仓腿保护：张数=本笔已成交（accFillSz，无则 remain），不撤旧保护单。"""
    sz = _num(e.get("accFillSz")) or _num(e.get("sz")) or _num(e.get("remain"))
    if sz <= 0:
        X.log_event("entry_limit_add_protect_skip", algoId=aid, inst_id=e["inst"], why="无成交张数")
        return None
    old_ids = {str(a.get("algoId")) for a in _close_algos_for(e)}
    new_id = _place_oco({**e, "aid": aid}, X._dec(sz))
    still = {str(a.get("algoId")) for a in _close_algos_for(e)}
    X.log_event("entry_limit_add_leg_protect", algoId=aid, inst_id=e["inst"], new_algoId=new_id, sz=X._dec(sz),
                old_kept=sorted(old_ids & still), old_lost=sorted(old_ids - still))
    return new_id


# ------------------------------------------------------------------ 等待/重挂（fallback_market=false）
def _resolve_condition_expires(aid, e):
    """重挂所需的触发条件（gte/lte）与原到期时刻：优先本条登记，其次 demo_orders 登记表
    （触发后 watch 处理完会 pop，只能尽力），再次按 decision_id 查决策记录的 pending。任一来源命中即用。"""
    cond, exp = e.get("condition"), e.get("expires")
    if cond not in ("gte", "lte") or not exp:
        try:
            from desk import demo_exec as DX
            m = (DX.reg().get("triggers") or {}).get(str(aid)) or {}
            if cond not in ("gte", "lte"):
                cond = m.get("condition")
            exp = exp or m.get("expires")
        except Exception:
            pass
    if (cond not in ("gte", "lte") or not exp) and e.get("decision_id"):
        try:
            from desk import manager
            for r in settings.read_jsonl(manager.DECISIONS):
                if r.get("decision_id") == e.get("decision_id"):
                    p = r.get("pending") or {}
                    if cond not in ("gte", "lte"):
                        cond = p.get("condition")
                    exp = exp or p.get("expires")
        except Exception:
            pass
    return cond, exp


def _wait_or_end(cfg, aid, e, filled, remain) -> str:
    """fallback_market=false 的超时收尾：绝不市价追单。
    remain<=0 → nothing_left；护栏不过 → 与旧 blocked 相同收尾（已成交部分照常保护）；
    价格已穿过触发价（条件当前成立）/ 原到期已过 / 条件或到期无法确定 → entry_missed_no_chase，本次入场作罢；
    价格回到等待侧 → submit_trigger 以原触发价/止损止盈/剩余张数重挂（原到期、原 decision_id），
    本条 done(result=rearmed, rearmed_to=新 algoId)，新单照常登记跟踪，可多轮重挂但始终受原到期约束。"""
    inst = e["inst"]
    if remain <= 0:
        _update(aid, status="done", result="nothing_left", accFillSz=X._dec(filled))
        return "blocked"
    reasons = _guard(cfg, {**e, "accFillSz": X._dec(filled), "remain": X._dec(remain)})
    if reasons:
        X.log_event("entry_limit_fallback_skipped", algoId=aid, inst_id=inst, remain=X._dec(remain), reasons=reasons)
        if filled > 0:
            _protect_total(aid, {**e})
        _update(aid, status="done", result="blocked", accFillSz=X._dec(filled))
        return "blocked"
    if filled > 0:
        _protect_total(aid, {**e})
    trigger = _num(e.get("trigger_px"))
    try:
        spot = float(X.get("/api/v5/market/ticker", instId=inst)["data"][0]["last"])
    except Exception as ex:
        X.log_event("entry_missed_no_chase", algoId=aid, inst_id=inst, trigger_px=trigger, spot=None,
                    reason=f"现价读取失败（{type(ex).__name__}），无法确认等待侧，不重挂")
        _update(aid, status="done", result="missed_no_chase", accFillSz=X._dec(filled))
        return "missed"
    cond, expires = _resolve_condition_expires(aid, e)
    exp_dt = None
    if expires:
        try:
            exp_dt = _dt.datetime.strptime(str(expires), "%Y-%m-%d %H:%M:%S%z")
        except Exception:
            exp_dt = None
    if cond not in ("gte", "lte") or not expires or exp_dt is None or settings.now() >= exp_dt:
        why = ("原单已过有效期" if (exp_dt is not None and settings.now() >= exp_dt)
               else "触发条件/原到期无法确定，不重挂")
        X.log_event("entry_missed_no_chase", algoId=aid, inst_id=inst, trigger_px=trigger, spot=spot,
                    condition=cond, expires=expires, reason=why)
        _update(aid, status="done", result="missed_no_chase", accFillSz=X._dec(filled))
        return "missed"
    waiting = (spot < trigger) if cond == "gte" else (spot > trigger)
    if not waiting:
        X.log_event("entry_missed_no_chase", algoId=aid, inst_id=inst, trigger_px=trigger, spot=spot,
                    deviation=round(spot - trigger, 6), condition=cond)
        _update(aid, status="done", result="missed_no_chase", accFillSz=X._dec(filled))
        return "missed"
    from desk import demo_exec as DX
    n = int(e.get("rearms") or 0) + 1
    o = {"decision_id": e.get("decision_id"), "source": e.get("source"), "side": e["side"],
         "leverage": float(e.get("lever") or 1), "trigger_px": trigger, "condition": cond,
         "stop_loss": e.get("stop_loss"), "take_profit": e.get("take_profit"),
         "size_pct": e.get("size_pct"), "horizon_days": e.get("horizon_days"),
         "consensus": e.get("consensus"), "reason": e.get("reason")}
    try:
        meta = DX.submit_trigger(cfg, inst, o, X._dec(remain), expires,
                                 extra={"rearm_of": str(aid), "rearm_n": n}, keep_lev=True)
    except Exception as ex:
        X.log_event("entry_rearm_failed", algoId=aid, inst_id=inst, error=f"{type(ex).__name__}: {ex}")
        _update(aid, status="done", result="rearm_failed", accFillSz=X._dec(filled))
        return "rearm_failed"
    new_id = str(meta["algoId"])
    _update(new_id, rearms=n, rearm_of=str(aid))
    X.log_event("entry_rearmed", algoId=aid, new_algoId=new_id, inst_id=inst, trigger_px=trigger, spot=spot,
                remain=X._dec(remain), expires=expires, rearms=n, condition=cond)
    _update(aid, status="done", result="rearmed", rearmed_to=new_id, accFillSz=X._dec(filled))
    return "rearmed"


# ------------------------------------------------------------------ 主流程（demo_watch 每轮调用）
def check_timeouts(cfg, now_ms=None) -> list:
    from desk import demo_exec as DX
    el = DX.ecfg(cfg)["entry_limit"]
    out = []
    with _locked() as ok:
        if not ok:
            X.log_event("entry_limit_skip_locked"); return out
        for aid, e in list(_load()["entries"].items()):
            if e.get("status") == "done":
                continue
            try:
                from desk import exec_lock
                with exec_lock.hold(e["inst"], why="entry_limit"):   # 与日常/盯盘同币执行互斥
                    out.append((aid, _step(cfg, el, aid, e, now_ms or int(time.time() * 1000))))
            except Exception as ex:
                X.log_event("entry_limit_error", algoId=aid, inst_id=e.get("inst"), error=f"{type(ex).__name__}: {ex}")
                out.append((aid, "error"))
    return out


def _step(cfg, el, aid, e, now_ms) -> str:
    inst = e["inst"]
    st = e.get("status")
    if st == "armed":
        h = X.get("/api/v5/trade/orders-algo-history", ordType="trigger", algoId=aid).get("data", [])
        if not h:
            return "armed"   # 仍在挂单中（pending 不在 history）
        a = h[0]; state = a.get("state")
        if state == "effective":
            oid = a.get("ordId") or (a.get("ordIdList") or [None])[0]
            tms = e.get("triggered_ms") or int(_num(a.get("triggerTime")) or _num(a.get("uTime")) or now_ms)
            e = _update(aid, status="triggered", ordId=str(oid) if oid else e.get("ordId"), triggered_ms=tms)
            X.log_event("entry_limit_triggered", algoId=aid, inst_id=inst, ordId=e.get("ordId"), triggered_ms=tms)
            st = "triggered"
        elif state in ("canceled", "order_failed"):
            _update(aid, status="done", result=f"algo_{state}")
            X.log_event("entry_limit_algo_gone", algoId=aid, inst_id=inst, state=state)
            return "gone"
        else:
            return "armed"
    if st == "triggered":
        if not e.get("ordId"):
            X.log_event("entry_limit_no_ordid", algoId=aid, inst_id=inst); return "triggered"
        o = _get_order(inst, ord_id=e["ordId"])
        if o and o.get("state") == "filled":
            _update(aid, status="done", result="filled", accFillSz=o.get("accFillSz"))
            X.log_event("entry_limit_filled", algoId=aid, inst_id=inst, ordId=e["ordId"], accFillSz=o.get("accFillSz"))
            return "filled"
        if now_ms - int(e["triggered_ms"]) < float(el["timeout_minutes"]) * 60000:
            return "waiting"
        e = _update(aid, status="cancelling")
        X.log_event("entry_limit_timeout", algoId=aid, inst_id=inst, ordId=e["ordId"], state=(o or {}).get("state"),
                    accFillSz=(o or {}).get("accFillSz"))
        st = "cancelling"
    if st == "cancelling":
        o = _get_order(inst, ord_id=e["ordId"])
        if o and o.get("state") in ("live", "partially_filled"):
            X.post("/api/v5/trade/cancel-order", {"instId": inst, "ordId": e["ordId"]})
            X.log_event("entry_limit_child_cancelled", algoId=aid, inst_id=inst, ordId=e["ordId"])
            o = _get_order(inst, ord_id=e["ordId"])
        if o and o.get("state") in ("live", "partially_filled"):
            return "cancelling"   # 撤单尚未生效，下轮再查
        filled = _num((o or {}).get("accFillSz"))
        total = _num((o or {}).get("sz")) or _num(e.get("sz"))
        if o and o.get("state") == "filled" or filled >= total - 1e-12:
            _update(aid, status="done", result="filled", accFillSz=X._dec(filled))
            X.log_event("entry_limit_filled", algoId=aid, inst_id=inst, ordId=e["ordId"], accFillSz=X._dec(filled), note="撤单前已完全成交")
            return "filled"
        lot = float(_ct(inst)["lotSz"])
        remain = round((total - filled) / lot) * lot
        if not el.get("fallback_market"):
            # 2026-11-11 用户决定：取消市价兜底，就等它触发（等待/重挂语义，见 _wait_or_end）
            return _wait_or_end(cfg, aid, e, filled, remain)
        reasons = _guard(cfg, {**e, "accFillSz": X._dec(filled), "remain": X._dec(remain)})
        if reasons or remain <= 0:
            X.log_event("entry_limit_fallback_skipped", algoId=aid, inst_id=inst, remain=X._dec(remain), reasons=reasons)
            if filled > 0:
                _protect_total(aid, {**e})
            _update(aid, status="done", result="blocked" if reasons else "nothing_left", accFillSz=X._dec(filled))
            return "blocked"
        # 兜底前判定（2026-11-11 用户指令，desk/fallback_judge.py）：护栏通过后、市价补单发出前，
        # 由交易经理身份 + 快速模型判断当前价位入场是否仍合理。结论先落盘（fallback_judge 字段）再执行，
        # 重入/重启不重复判定；已记录 proceed 时本段整段跳过，直接走原 filling 路径。
        prev_judge = e.get("fallback_judge") or {}
        if prev_judge.get("verdict") not in ("proceed", "abandon"):
            from desk import fallback_judge
            if fallback_judge.judge_cfg(cfg)["enabled"]:
                outcome, facts, judgment = fallback_judge.judge_fallback(
                    cfg, e, filled=filled, remain=remain, now_ms=now_ms)
                _update(aid, fallback_judge={"verdict": outcome, "judgment": judgment, "facts": facts,
                                             "at": settings.now_str()})
                prev_judge = {"verdict": outcome}
                if outcome == "proceed":
                    X.log_event("fallback_judge_proceed", algoId=aid, inst_id=inst,
                                reason=(judgment or {}).get("reason"), **facts)
                elif outcome == "skip":
                    X.log_event("fallback_judge_skipped", algoId=aid, inst_id=inst, **facts)
                    prev_judge = {"verdict": "proceed"}   # 现价取不到（见 fallback_judge 模块头注）：按旧行为放行
                else:   # abandon / failed（failed 按未批准处理）：不发市价单，与 blocked 同样收尾
                    if outcome == "failed":
                        X.log_event("fallback_judge_failed", algoId=aid, inst_id=inst, **facts)
                    X.log_event("fallback_abandoned", algoId=aid, inst_id=inst,
                                judge_outcome=outcome, reason=(judgment or {}).get("reason"), **facts)
                    if filled > 0:
                        _protect_total(aid, {**e})
                    _update(aid, status="done", result="abandoned", accFillSz=X._dec(filled))
                    return "abandoned"
        elif prev_judge.get("verdict") == "abandon":   # 判定已落盘否决但上次未及收尾（防御）：直接收尾
            if filled > 0:
                _protect_total(aid, {**e})
            _update(aid, status="done", result="abandoned", accFillSz=X._dec(filled))
            return "abandoned"
        e = _update(aid, status="filling", accFillSz=X._dec(filled), remain=X._dec(remain),
                    fallback_cl=_det("elf", aid, X._dec(remain)), attach_new=filled <= 0)
        st = "filling"
    if st == "filling":
        if _get_order(inst, cl=e["fallback_cl"]) is None:   # 未下过 → 下；已存在则不重复
            body = {"instId": inst, "tdMode": "isolated", "side": "buy" if e["side"] == "long" else "sell", "ordType": "market",
                    "sz": e["remain"], "clOrdId": e["fallback_cl"]}
            if e.get("pos_side", "net") != "net":
                body["posSide"] = e["pos_side"]
            if e.get("attach_new"):   # 原子单一点没成交 → 原附带止损止盈不会生成，给补单附带相同的
                from desk import demo_exec as DX
                body["attachAlgoOrds"] = [DX._attach(inst, e.get("stop_loss"), e.get("take_profit"))]
            d = X.post("/api/v5/trade/order", body)["data"][0]
            X.log_event("entry_limit_fallback_placed", algoId=aid, inst_id=inst, ordId=d.get("ordId"), clOrdId=e["fallback_cl"],
                        sz=e["remain"], side=e["side"], attach=bool(e.get("attach_new")))
        else:
            X.log_event("entry_limit_fallback_exists", algoId=aid, inst_id=inst, clOrdId=e["fallback_cl"])
        e = _update(aid, status="done" if e.get("attach_new") else "protecting",
                    result="fallback_done" if e.get("attach_new") else None)
        if e.get("attach_new"):
            return "fallback_done"
        st = "protecting"
    if st == "protecting":
        fo = _get_order(inst, cl=e["fallback_cl"])
        if fo and fo.get("state") not in ("filled", "canceled", "mmp_canceled"):
            return "protecting"   # 市价补单尚未终态，下轮再挂
        new_id = _protect_total(aid, e)
        _update(aid, status="done", result="fallback_done", sltp_algoId=new_id)
        return "fallback_done"
    return st or "unknown"
