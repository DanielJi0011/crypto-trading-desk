"""条件入场单「触发后限价」的超时处理（execution.entry_limit）。

!! 禁止实盘：所有调用都经 desk/okx_demo.py（OKX_DEMO_* + x-simulated-trading: 1）。只处理本模块登记过的条件单
!! （place_trigger 在 entry_limit.enabled 时登记），不碰其它任何现有订单。

状态文件 state/ledger/entry_limit.json：entries[algoId] = {...,status}
  armed      已挂条件单，未触发
  triggered  已触发，记录 ordId（限价子单）与 triggered_ms（触发时间；demo_ws 收到触发事件时先记，否则取算法单历史 triggerTime/uTime）
  cancelling 已决定撤限价子单（先落盘再发请求）
  filling    已生成市价补单 clOrdId（先落盘再下单；重入时按 clOrdId 查单，不会重复补单）
  protecting 补单已完成，正在按总仓位重挂止损止盈
  done       终态（filled / fallback_done / blocked / gone 等，见 result）
防重入：fcntl 文件锁 state/ledger/entry_limit.lock（非阻塞，拿不到锁本轮跳过）+ 上述状态机。
"""
from __future__ import annotations

import contextlib
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
        reasons = [] if el.get("fallback_market") else ["fallback_market=false"]
        reasons += _guard(cfg, {**e, "accFillSz": X._dec(filled), "remain": X._dec(remain)}) if not reasons else []
        if reasons or remain <= 0:
            X.log_event("entry_limit_fallback_skipped", algoId=aid, inst_id=inst, remain=X._dec(remain), reasons=reasons)
            if filled > 0:
                _protect_total(aid, {**e})
            _update(aid, status="done", result="blocked" if reasons else "nothing_left", accFillSz=X._dec(filled))
            return "blocked"
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
