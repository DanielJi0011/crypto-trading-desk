"""模拟盘轻量轮询（每 5 分钟，不调用 LLM）：
  0) 限价入场超时（execution.entry_limit）：撤未成交限价子单、市价补剩余并保证止损止盈覆盖全仓；
  1) 条件入场单：已成交 → 记录持有期到期时间；超过有效期 → 撤单（execution.protected_algo_ids 内的不撤）；
  2) 持仓持有期到期 → 模拟盘平仓；
  3) 从模拟盘算法单历史检测止损或止盈（actualSide=sl/tp）→ 按原规则后台启动该币全角色再讨论（冷却 pending.rediscuss_cooldown_hours）。
!! 禁止实盘：只经 desk/okx_demo.py 访问模拟盘（OKX_DEMO_* + x-simulated-trading: 1）。不写本地纸面账本 / pending。

用法：python -m desk.demo_watch --once | --loop 300"""
from __future__ import annotations

import argparse
import signal
import datetime as _dt
import time as _time
import sys
import time
import traceback

from desk import demo_exec as DX
from desk import okx_demo as X
from desk import settings
from desk import entry_state as ES
from desk import expiry as EXP


RESCAN_MS = 24 * 3600 * 1000   # 止损/止盈回扫窗口：最近 24 小时


def log(msg):
    print(f"[demo_watch {settings.now_str()}] {msg}", flush=True)


def _notify(kind, **kw):
    """Q5（用户决定）：过期只通知，不触发再讨论。通知失败不影响本轮。"""
    try:
        from desk.demo_ws import notify
        notify({"kind": kind, **kw})
    except Exception as e:
        log(f"通知 {kind} 失败 {type(e).__name__}")


def _parse(s):
    return _dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S%z")


def _expiry(cfg, r, inst, m, pos, now):
    """2026-10-29 用户决定：到期（或到期前一个检查周期内）由交易经理判断延期或平仓；失败/达上限回退旧行为（到期平仓）。
    结论与通知标记先写入登记再执行，重启后不重复判断、不重复通知。平仓后只通知，不触发再讨论。"""
    r.setdefault("positions", {})[inst] = m   # r 可能已在本轮被重新读取，确保写回的是当前这条登记
    if EXP.mode(cfg) == "full_discussion":
        return _expiry_full(cfg, r, inst, m, pos, now)
    v = EXP.judge(cfg, inst, pos, m, now)
    DX.save_reg(r)                        # 先落盘结论（含跨轮尝试计数）
    if v["action"] == "extend":
        EXP.apply_extend(m, v); DX.save_reg(r)
        X.log_event("expiry_extended", inst_id=inst, days=v["days"], new_deadline=m["deadline"],
                    orig_deadline=m.get("orig_deadline"), extensions=m["extensions"], reason=v["reason"])
        log(f"{inst} 持有期延期 {v['days']:g} 天至 {m['deadline']}：{v['reason']}")
        _notify("position_extended", inst_id=inst,
                message=f"{inst} 持有期到期：交易经理延期 {v['days']:g} 天（新到期 {m['deadline']}，止损止盈不变），原因：{v['reason']}")
        return r
    if now < _parse(m["deadline"]):       # 提前窗口内判了平仓：等真正到期再平（结论已记，不重复调用）
        return r
    DX.close(cfg, pos, "持有期到期"); log(f"{inst} 持有期到期，已平仓：{v['reason']}")
    X.log_event("expiry_closed", inst_id=inst, deadline=v.get("deadline"), reason=v["reason"])
    _notify("position_expired", inst_id=inst, message=f"{inst} 持有期到期，已平仓（{v['reason']}；只通知，不再讨论）")
    return DX.reg()


def _expiry_close(cfg, inst, pos, deadline, reason, now, **ev):
    if now < _parse(deadline):          # 提前窗口内：等真正到期再平
        return None
    DX.close(cfg, pos, "持有期到期"); log(f"{inst} 持有期到期，已平仓：{reason}")
    X.log_event("expiry_closed", inst_id=inst, deadline=deadline, reason=reason, mode="full_discussion", **ev)
    _notify("position_expired", inst_id=inst, message=f"{inst} 持有期到期，已平仓（{reason}）")
    return DX.reg()


def _expiry_full(cfg, r, inst, m, pos, now):
    """2026-10-30 用户决定：到期对该币跑一次与早晚报相同的全员讨论（后台子进程 desk.expiry_discuss，不阻塞本轮与止损检查；
    讨论期间原止损止盈保护单不动）。每个 deadline 只触发一次：expiry_discussed_for 标记先落盘再启动。
    结果由本进程（登记唯一写者）应用：持有/调整 → 延期（deadline=原到期+本次决策 horizon_days）；失败/超时 → 回退到期平仓；
    达 max_extensions → 不讨论直接平仓。讨论结论为平仓的，由组合经理在讨论进程内平仓并通知。"""
    from desk import expiry_discuss as ED
    c, key = EXP.ecfg(cfg), m["deadline"]
    if m.get("expiry_discussed_for") != key:
        n = int(m.get("extensions") or 0)
        if n >= int(c["max_extensions"]):
            return _expiry_close(cfg, inst, pos, key, f"已达延期上限（{n} 次），不再讨论，到期平仓", now) or r
        if ED.schedule(cfg, r, inst, m, DX.save_reg):
            X.log_event("expiry_discussion_started", inst_id=inst, deadline=key, extensions=n)
            log(f"{inst} 持有期到期 {key}：后台启动全员讨论")
            _notify("expiry_discussion_started", inst_id=inst,
                    message=f"{inst} 持有期到期（{key}）：开始全员讨论（延期/平仓/调整），讨论期间止损止盈保护单不变")
        return r
    d = m.get("expiry_discussion") or {}
    if d.get("applied") == key:
        return r
    res = ED.read_result(inst, key)
    if res is None:
        try:
            started = _parse(d.get("started") or key)
        except Exception:
            started = now
        if now - started < _dt.timedelta(minutes=float(c["discussion_timeout_minutes"])):
            return r                      # 讨论进行中
        res = ED.write_result_once(inst, key, {"status": "failed", "reason": f"全员讨论超过 {c['discussion_timeout_minutes']} 分钟无结果，回退到期平仓"})
    if res.get("status") == "done" and res.get("outcome") == "closed":
        d["applied"] = key; m["expiry_discussion"] = d; DX.save_reg(r)
        return r                          # 已在讨论进程内平仓并通知；登记由持仓消失分支清理
    if res.get("status") == "done":
        days = float(res.get("horizon_days") or 5) or 5.0
        nd = (_parse(key) + _dt.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S%z")
        EXP.apply_extend(m, {"deadline": key, "new_deadline": nd, "days": days, "reason": res.get("reason"), "time": settings.now_str()})
        m["horizon_days"] = days
        d.update(applied=key, status="done"); m["expiry_discussion"] = d
        DX.save_reg(r)
        X.log_event("expiry_extended", inst_id=inst, days=days, new_deadline=nd, orig_deadline=m.get("orig_deadline"),
                    extensions=m["extensions"], reason=res.get("reason"), mode="full_discussion", outcome=res.get("outcome"),
                    final_action=res.get("final_action"), consensus=res.get("consensus"), decision_id=res.get("decision_id"))
        log(f"{inst} 全员讨论：{res.get('reason')} → 延期 {days:g} 天至 {nd}")
        _notify("position_extended", inst_id=inst,
                message=f"{inst} 持有期到期全员讨论：{res.get('reason')}，延期 {days:g} 天（新到期 {nd}，决策 {res.get('decision_id')}）")
        return r
    if res.get("status") == "gone":
        d["applied"] = key; m["expiry_discussion"] = d; DX.save_reg(r)
        return r
    # failed：回退旧行为（到期平仓）
    out = _expiry_close(cfg, inst, pos, key, res.get("reason") or "全员讨论失败，回退到期平仓", now, discussion_failed=True)
    if out is None:
        return r
    return out


def run_once(cfg, source: str = "demo_watch 轮询") -> dict:
    if not X.verify():
        log("模拟环境自检失败：本轮不做任何操作"); return {"ok": False}
    now = settings.now()
    halt = settings.halt_active()   # R7：HALT 全冻结 → 本轮只读取与通知，不做任何自动写操作
    if halt:
        log(f"HALT 全冻结生效（{settings.HALT_PATH}）：本轮只读取/通知，跳过限价补单、过期撤单、到期平仓、再讨论")
    # R10：KILL/HALT/当日亏损/回撤 → 撤入场类条件单与在途入场子单（含受保护单，不动止损止盈）。demo_ws 另有 30 秒检查。
    try:
        from desk import trigger_review
        rv = trigger_review.poll(cfg)
        if rv.get("reasons"):
            log(f"风险停机 {rv['reasons']}：撤入场条件单 {len(rv.get('algos', []))}、入场子单 {len(rv.get('orders', []))}，失败 {len(rv.get('errors', []))}")
    except Exception as e:
        log(f"风险复核失败 {type(e).__name__}: {e}")
        X.log_event("trigger_review_error", error=str(e)[:300])
    # 0) 限价入场超时：触发后限价子单超过 entry_limit.timeout_minutes 未完全成交 → 撤单 + 市价补剩余（desk/entry_limit.py）
    try:
        from desk import entry_limit
        for aid, res in ([] if halt else entry_limit.check_timeouts(cfg)):
            if res not in ("armed", "waiting"):
                log(f"限价入场 {aid}: {res}")
    except Exception as e:
        log(f"限价入场超时检查失败 {type(e).__name__}: {e}")
        X.log_event("entry_limit_error", error=str(e))
    r = DX.reg()
    live = {t["algoId"]: t for t in DX.trigger_orders()}
    prot = {str(x) for x in DX.ecfg(cfg)["protected_algo_ids"]}
    # 1) 条件单
    for aid, m in list(r["triggers"].items()):
        if aid in live:
            if m.get("expires") and now >= _parse(m["expires"]):
                if aid in prot:
                    log(f"条件单 {aid} 已过有效期但受保护，保留")
                elif halt:
                    log(f"条件单 {aid} 已过有效期，HALT 生效不撤（停机撤单见 R10 复核）")
                else:
                    X.post("/api/v5/trade/cancel-algos", [{"instId": m["inst_id"], "algoId": aid}])
                    X.log_event("trigger_expired", inst_id=m["inst_id"], algoId=aid); r["triggers"].pop(aid)
                    _adv(aid, "expired", inst_id=m["inst_id"])
                    log(f"条件单 {aid} {m['inst_id']} 过期撤销")
                    _notify("trigger_expired", inst_id=m["inst_id"], algoId=aid, message=f"条件单 {aid} 已过有效期并撤销（只通知，不再讨论）")
            continue
        h = X.get("/api/v5/trade/orders-algo-history", ordType="trigger", algoId=aid).get("data", [])
        state = h[0].get("state") if h else "unknown"
        if state == "effective":
            # R12：effective 只代表触发，不代表成交。记 trigger_fired，并记下子单 ordId，成交由子单状态确认（entry_filled）
            child = h[0].get("ordId") or (h[0].get("ordIdList") or [None])[0]
            if m.get("add"):
                # 条件加仓单：保留原开仓登记，成交后由 add_position.on_fill 复核预算并确认本腿独立保护单（不撤原仓保护）
                r.setdefault("add_fills", {})[aid] = {**m["add"], "inst_id": m["inst_id"], "child_ordId": child,
                                                      "decision_id": m.get("decision_id"), "fired": settings.now_str()}
                r["positions"].setdefault(m["inst_id"], {"decision_id": m.get("decision_id"), "side": m.get("side"),
                                                         "opened": settings.now_str()}).setdefault("adds", []).append(
                    {"decision_id": m.get("decision_id"), "from_algo": aid, "child_ordId": child, "time": settings.now_str()})
                X.log_event("add_trigger_fired", inst_id=m["inst_id"], algoId=aid, ordId=child)
                _adv(aid, "triggered", inst_id=m["inst_id"], child_ordId=child)
                log(f"条件加仓单 {aid} {m['inst_id']} 已触发（子单 {child}），成交后复核预算")
                r["triggers"].pop(aid)
                continue
            r["positions"][m["inst_id"]] = {"decision_id": m.get("decision_id"), "side": m.get("side"), "opened": settings.now_str(),
                                            "from_algo": aid, "child_ordId": child, "horizon_days": float(m.get("horizon_days") or 5),
                                            "deadline": (now + _dt.timedelta(days=float(m.get("horizon_days") or 5))).strftime("%Y-%m-%d %H:%M:%S%z")}
            X.log_event("trigger_fired", inst_id=m["inst_id"], algoId=aid, ordId=child)
            _adv(aid, "triggered", inst_id=m["inst_id"], child_ordId=child)
            log(f"条件单 {aid} {m['inst_id']} 已触发（子单 {child}，成交以子单状态为准）")
        else:
            X.log_event("trigger_gone", inst_id=m["inst_id"], algoId=aid, state=state)
            if state in ("canceled", "order_failed"):
                _adv(aid, "canceled", inst_id=m["inst_id"])
        r["triggers"].pop(aid)
    DX.save_reg(r)
    # 2) 持有期到期
    bk = DX.book(cfg)
    held = {p["inst_id"]: p for p in bk["positions"] if not p.get("legacy")}
    # 持仓新增/消失 → 开/平仓通知（与 ws 推送共用 posId+cTime 去重；首次只建基线）
    try:
        from desk import demo_ws
        demo_ws.sync_snapshot(DX.demo_positions(), source)
    except Exception as e:
        log(f"持仓变化检测失败 {type(e).__name__}: {e}")
    for inst, m in list(r["positions"].items()):
        _entry_filled(inst, m)
        if inst not in held:
            # R12：不再按“10 分钟无持仓即删登记”处理；查询入场子单终态后再决定
            verdict = _child_verdict(inst, m)
            if verdict in ("live", "unknown"):
                continue
            X.log_event("registry_position_cleared", inst_id=inst, child_ordId=m.get("child_ordId"), verdict=verdict)
            k = _es_key(m)
            if ES.get(k) in ("filled", "partial"):
                if ES.get(k) == "partial":
                    _adv(k, "filled", inst_id=inst)
                _adv(k, "closed", inst_id=inst, verdict=verdict)
            elif ES.get(k) in ("triggered", "open_sent") and verdict == "canceled":
                _adv(k, "canceled", inst_id=inst)
            r["positions"].pop(inst)
            continue
        if m.get("deadline") and EXP.due(cfg, m, now):
            if halt:   # HALT：不平仓，也不调用交易经理（只读）
                if now >= _parse(m["deadline"]):
                    log(f"{inst} 持有期到期，HALT 生效不平仓"); X.log_event("halt_skip_close", inst_id=inst)
                continue
            r = _expiry(cfg, r, inst, m, held[inst], now)
    DX.save_reg(r)
    # 2a) 条件加仓单成交复核（预算 + 本腿独立保护单），须在止损覆盖检查之前
    if r.get("add_fills"):
        try:
            from desk import add_position
            for aid, res in add_position.on_fill(cfg, r):
                log(f"条件加仓 {aid} 成交复核：{res}")
        except Exception as e:
            log(f"条件加仓复核失败 {type(e).__name__}: {e}")
        DX.save_reg(r)
    # 2b) R11 止损覆盖：每个持仓必须有覆盖全部张数的止损（缺失告警；按配置自动重挂）
    try:
        from desk import stop_coverage
        for k, res in stop_coverage.check(cfg, halt=halt):
            if res != "ok":
                log(f"止损覆盖 {k}: {res}")
    except Exception as e:
        log(f"止损覆盖检查失败 {type(e).__name__}: {e}")
        X.log_event("stop_coverage_error", error=str(e)[:300])
    # 2c) R11 意图对账：结果不明的下单按 clOrdId 向交易所核实（查到即告警，不自动撤）
    try:
        from desk import reconcile
        for cl, res in reconcile.run():
            log(f"下单对账 {cl}: {res}")
    except Exception as e:
        log(f"下单对账失败 {type(e).__name__}: {e}")
        X.log_event("reconcile_error", error=str(e)[:300])
    # 3) 止损/止盈检测 → 再讨论
    from desk.monitor import schedule_rediscuss
    cur = int(r["watch"].get("last_algo_uts") or int(now.timestamp() * 1000))
    newest = cur
    # 回扫最近 RESCAN_MS 内已成交的止损/止盈单，按 algoId 去重（handled_algos），不再只靠单一水位：
    # 任一处理方（例如旧代码进程）推过水位也不会吞掉事件。首次上线把水位以内的历史单一次性标为已处理，避免重跑旧事件。
    first = "handled_algos" not in r["watch"]
    handled = r["watch"].setdefault("handled_algos", {})
    floor = int(now.timestamp() * 1000) - RESCAN_MS
    for t in ("oco", "conditional"):
        for a in X.get("/api/v5/trade/orders-algo-history", ordType=t, state="effective", instType="SWAP").get("data", []):
            u = int(a.get("uTime") or 0)
            is_close = a.get("actualSide") in ("sl", "tp")
            if is_close and first and u <= cur:
                handled[a["algoId"]] = u
                continue
            if u <= cur and not (is_close and u >= floor and a["algoId"] not in handled):
                continue
            newest = max(newest, u)
            if is_close:
                if a["algoId"] in handled:
                    continue
                handled[a["algoId"]] = u
                why = "止损" if a["actualSide"] == "sl" else "止盈"
                X.log_event("stop_detected" if a["actualSide"] == "sl" else "tp_detected", inst_id=a["instId"], algoId=a["algoId"], px=a.get("actualPx"))
                if a["actualSide"] == "sl":   # 止盈不记止损冷却（不禁止同向）
                    try:   # L6：记下被止损方向
                        from desk import stop_cooldown
                        stop_cooldown.record(a["instId"], a.get("side"), a.get("posSide"), a.get("algoId"),
                                             now=(int(a.get("uTime") or 0) / 1000) or None)
                    except Exception as e:
                        log(f"止损冷却记录失败 {type(e).__name__}: {e}")
                from desk import drill
                if drill.active(a["instId"]):
                    log(f"检测到演练合约{why} {a['instId']} algo {a['algoId']}，不启动再讨论"); continue
                if halt:
                    log(f"检测到模拟盘{why} {a['instId']} algo {a['algoId']}，HALT 生效不启动再讨论"); continue
                log(f"检测到模拟盘{why} {a['instId']} algo {a['algoId']} → 再讨论：{schedule_rediscuss(cfg, {'inst_id': a['instId'], 'trade_id': 'DEMO-' + a['algoId'], 'reason': a['actualSide']})}")
            else:
                X.log_event("close_algo_filled", inst_id=a["instId"], algoId=a["algoId"], side=a.get("actualSide"))
    keep = {k: v for k, v in handled.items() if v >= floor - RESCAN_MS}   # 只保留 48 小时内的已处理记录
    r = DX.reg(); r["watch"]["last_algo_uts"] = max(newest, int(r["watch"].get("last_algo_uts") or 0))
    r["watch"]["handled_algos"] = {**{k: v for k, v in (r["watch"].get("handled_algos") or {}).items() if v >= floor - RESCAN_MS}, **keep}
    DX.save_reg(r)
    log(f"完成：模拟盘持仓 {len(held)}，条件单 {len(live)}，权益基准 {bk['equity']:.2f}（实时 {bk['live_equity']:.2f}）")
    return {"ok": True}


def _child_verdict(inst, m) -> str:
    """入场子单终态判定：live（未完结，保留登记）/ filled（已成交，持仓已平）/ canceled（撤单，可能部分成交）/
    no_child（旧登记无子单号且该币无在途入场子单）/ unknown（查询失败，保留登记）。"""
    try:
        if not m.get("child_ordId"):
            return "live" if DX.pending_entry_orders(inst) else "no_child"
        d = X.get("/api/v5/trade/order", instId=inst, ordId=str(m["child_ordId"])).get("data") or []
        st = (d[0] if d else {}).get("state")
        if st in ("live", "partially_filled"):
            return "live"
        if st == "filled":
            return "filled"
        if st in ("canceled", "mmp_canceled"):
            return "canceled"
        return "unknown"
    except Exception as e:
        log(f"{inst} 子单状态查询失败 {type(e).__name__}，保留登记")
        return "unknown"


def _adv(k, new, **info):
    """只推进状态机上线后登记过的入场（旧仓、旧条件单没有初始状态，不补记，避免每轮刷非法跳转）。"""
    if ES.get(k) is None:
        return False
    return ES.advance(k, new, **info)


def _es_key(m):
    return m.get("from_algo") or m.get("child_ordId")


def _entry_filled(inst, m):
    """R12：以子单实际成交确认 entry_filled（带成交量与均价），每笔只记一次。"""
    if not m.get("child_ordId") or m.get("entry_filled_logged"):
        return
    try:
        d = X.get("/api/v5/trade/order", instId=inst, ordId=str(m["child_ordId"])).get("data") or []
    except Exception:
        return
    o = d[0] if d else {}
    k = _es_key(m)
    if float(o.get("accFillSz") or 0) > 0:
        fin = o.get("state") in ("filled", "canceled", "mmp_canceled")
        if not fin and ES.get(k) != "partial":
            _adv(k, "partial", inst_id=inst, accFillSz=o.get("accFillSz"))
        elif fin and ES.get(k) not in ("filled", "closed"):
            _adv(k, "filled", inst_id=inst, accFillSz=o.get("accFillSz"), avgPx=o.get("avgPx"))
    elif o.get("state") in ("canceled", "mmp_canceled") and ES.get(k) in ("triggered", "open_sent"):
        _adv(k, "canceled", inst_id=inst)
    if float(o.get("accFillSz") or 0) > 0 and not m.get("deadline_from_fill"):
        # R12：持有期从首次成交起算（而不是触发时刻）；OKX 订单只给最近成交时间 fillTime，首次看到成交时取它
        ft = int(float(o.get("fillTime") or o.get("uTime") or 0)) or int(_time.time() * 1000)
        dl = _dt.datetime.fromtimestamp(ft / 1000, tz=settings.now().tzinfo) + _dt.timedelta(days=float(m.get("horizon_days") or 5))
        m["deadline"] = dl.strftime("%Y-%m-%d %H:%M:%S%z"); m["deadline_from_fill"] = True
        X.log_event("deadline_from_fill", inst_id=inst, ordId=m["child_ordId"], fill_ms=ft, deadline=m["deadline"])
    if float(o.get("accFillSz") or 0) > 0 and o.get("state") in ("filled", "canceled", "mmp_canceled"):
        X.log_event("entry_filled", inst_id=inst, ordId=m["child_ordId"], accFillSz=o.get("accFillSz"), avgPx=o.get("avgPx"),
                    state=o.get("state"), fill_time=o.get("fillTime") or o.get("uTime"))
        m["entry_filled_logged"] = True


ROUND_TIMEOUT = 120   # 单轮总超时（秒）：超时放弃本轮、写日志、继续下一轮


class RoundTimeout(Exception):
    pass


def _alarm(signum, frame):
    raise RoundTimeout()


def run_with_timeout(fn, seconds=ROUND_TIMEOUT):
    """主线程内用 SIGALRM 限制单轮总时长；非主线程（如 demo_ws 重连补查）直接运行（其网络请求均有 15s timeout）。"""
    import threading
    if threading.current_thread() is not threading.main_thread() or seconds <= 0:
        return fn()
    old = signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(int(seconds))
    try:
        return fn()
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", type=int, default=0)
    ap.add_argument("--source", default="demo_watch 轮询")   # demo_ws 重连补查以子进程调用时传入
    a = ap.parse_args(argv)
    while True:
        try:   # 离机心跳（每轮约 5 分钟一次，失败只告警，不影响本轮轮询；不记录 URL）
            from desk import offsite
            offsite.heartbeat()
        except Exception as e:
            log(f"离机心跳异常：{type(e).__name__}")
        try:
            run_with_timeout(lambda: run_once(settings.load_config(), source=a.source))
        except RoundTimeout:
            log(f"本轮超时（>{ROUND_TIMEOUT}s），放弃本轮，下一轮继续")
            X.log_event("watch_timeout", seconds=ROUND_TIMEOUT)
        except Exception as e:
            traceback.print_exc(); log(f"错误：{type(e).__name__}: {e}")
            X.log_event("watch_error", error=str(e))
        if a.once or not a.loop:
            return 0
        time.sleep(a.loop)


if __name__ == "__main__":
    sys.exit(main())
