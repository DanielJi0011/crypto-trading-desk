"""开仓后盈亏比（RR）复核（2026-11-11 用户决定补上）。

背景：RR≥risk.min_rr_after_adjust 只在下单时按【计划入场价】校验。条件单触发后限价子单可能 0 成交、
市价兜底在更差的价格成交（2026-11-11 ETH：计划触发 2075，限价子单 0 成交，市价兜底成交 2055.74），
止损止盈是下单时定的绝对价、不随实际成交价移动，仓位的实际 RR 被压缩到 ≈1.007 也无人发现，
直到晚场交易经理调点被 [RR] 退回才偶然暴露。本模块由 demo_watch 每轮调用：

- 首次运行只给当时已有持仓建基线（baseline），绝不追溯触发——上线前已开的仓（含上述 ETH 仓）不补讨论；
- 之后新观察到的开仓（以 posId+cTime 为键，与 demo_ws 持仓快照同一身份口径）进入 pending：
  取实际成交均价与当前附带止损止盈，用 sizing.rr_ratio 按【实际成交价】算有效 RR；
- RR ≥ 下限 → ok（只记事件）；RR < 下限（或止损/止盈侧向无效算不出 RR）→ 经 monitor.schedule_rediscuss
  以 reason="rr_recheck" 对该仓位至多触发一次再讨论，事实（计划价/成交价/计划 RR/实际 RR/现止损止盈
  与距离）经 rediscuss.json 的 details 传给全部角色，由交易经理决定调点/减仓/平仓/保留并说明理由
  ——本模块绝不自动改点位、不自动减仓、不自动平仓；
- 止损或止盈缺失 → 等待宽限（GRACE_SECONDS，覆盖保护单重挂窗口），超时仍缺失 → skipped（只记事件）；
  仓位在复核完成前平掉 → gone（只记事件）；HALT 时只登记不触发，解除后补触发；
- schedule_rediscuss 返回“启动失败”时保持 pending，下个巡检周期重试（其内部已回滚在途标记与冷却）。

开关：pending.rr_recheck_enabled=false 时不做任何检测与状态写入。RR 下限读 risk.min_rr_after_adjust，
与下单/调点护栏同一数值，不另设第二套。
状态文件 state/ledger/rr_recheck.json（fcntl 锁，与 demo_watch/demo_ws 补查并发安全）。
!! 只读模拟盘数据 + 写本地状态文件；除经 schedule_rediscuss 启动再讨论外不下单、不改单。
"""
from __future__ import annotations

import contextlib
import fcntl

from desk import okx_demo as X
from desk import settings
from desk import sizing

PATH = settings.STATE / "ledger" / "rr_recheck.json"
GRACE_SECONDS = 3600   # 止损/止盈缺失的宽限：保护单重挂（entry_limit protecting 等）通常一轮内完成


def enabled(cfg) -> bool:
    return bool((cfg.get("pending") or {}).get("rr_recheck_enabled", True))


def min_rr(cfg) -> float:
    return float(sizing.rcfg(cfg).get("min_rr_after_adjust", 1.5))


@contextlib.contextmanager
def _locked():
    PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(str(PATH) + ".lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _load() -> dict:
    return settings.read_json(PATH, {})


def _save(st: dict):
    settings.write_json(PATH, st)


# ---------------- 持仓身份（与 demo_ws.pos_item/_pkey 同一口径，此处自带一份避免耦合其通知状态）
def _pos_item(p: dict) -> dict:
    return {"pos_id": str(p.get("posId") or f"{p.get('instId')}:{p.get('posSide')}"),
            "c_time": str(p.get("cTime") or ""), "inst_id": p.get("instId"),
            "pos_side": p.get("posSide"), "sz": p.get("pos"), "avg_px": p.get("avgPx")}


def _pkey(it: dict) -> str:
    return f"{it['pos_id']}:{it['c_time']}"


def _side(it: dict) -> str:
    if it.get("pos_side") in ("long", "short"):
        return it["pos_side"]
    try:
        return "long" if float(it.get("sz") or 0) > 0 else "short"
    except (TypeError, ValueError):
        return "long"


def sltp_for(inst, pos_side):
    """当前附带止损/止盈（与 demo_exec.book 同一选取逻辑：该币该方向平仓算法单的第一条）。"""
    from desk import demo_exec as DX
    mine = [a for a in DX.close_algos(inst) if a.get("posSide") in (pos_side, "net")]
    sl = next((float(a["slTriggerPx"]) for a in mine if a.get("slTriggerPx")), None)
    tp = next((float(a["tpTriggerPx"]) for a in mine if a.get("tpTriggerPx")), None)
    return sl, tp


def _reg_meta(inst) -> dict:
    try:
        from desk import demo_exec as DX
        return (DX.reg().get("positions") or {}).get(inst) or {}
    except Exception:
        return {}


def planned_for(inst, side, reg_meta) -> dict:
    """计划入场价与计划 RR：优先入场限价登记（entry_limit，含触发价/限价/止损/止盈），其次触发单历史。
    查不到返回 {}（复核照常进行，简报里计划项留空）。计划 RR 按触发价算——与下单时 validate 的口径一致。"""
    out = {}
    aid = (reg_meta or {}).get("from_algo")
    if not aid:
        return out
    try:
        from desk import entry_limit
        e = (entry_limit._load().get("entries") or {}).get(str(aid))
    except Exception:
        e = None
    if e and e.get("trigger_px"):
        trig = float(e["trigger_px"])
        lp = e.get("order_px")
        out = {"planned_entry": trig, "limit_px": float(lp) if lp not in (None, "", "-1") else None,
               "planned_sl": float(e["stop_loss"]) if e.get("stop_loss") else None,
               "planned_tp": float(e["take_profit"]) if e.get("take_profit") else None}
        out["planned_rr"] = sizing.rr_ratio(side, trig, out["planned_sl"], out["planned_tp"])
        return out
    try:
        h = X.get("/api/v5/trade/orders-algo-history", ordType="trigger", algoId=str(aid)).get("data", [])
        a = h[0] if h else {}
        att = (a.get("attachAlgoOrds") or [{}])[0]
        sl = att.get("slTriggerPx") or a.get("slTriggerPx")
        tp = att.get("tpTriggerPx") or a.get("tpTriggerPx")
        if a.get("triggerPx"):
            out = {"planned_entry": float(a["triggerPx"]), "limit_px": None,
                   "planned_sl": float(sl) if sl else None, "planned_tp": float(tp) if tp else None}
            out["planned_rr"] = sizing.rr_ratio(side, float(a["triggerPx"]), out["planned_sl"], out["planned_tp"])
    except Exception:
        pass
    return out


def _schedule(cfg, trade):
    from desk.monitor import schedule_rediscuss
    return schedule_rediscuss(cfg, trade)


def check(cfg, positions=None, halt=False, now=None, schedule=None, sltp=None, planned=None) -> list:
    """一轮复核。返回给调用方打印的日志行。positions 缺省读模拟盘实时持仓（原始 OKX 字段）。"""
    if not enabled(cfg):
        return []
    now = now or settings.now()
    now_ts = now.timestamp()
    if positions is None:
        from desk import demo_exec as DX
        positions = DX.demo_positions()
    items = {}
    for p in positions:
        it = _pos_item(p)
        try:
            if not float(it["sz"] or 0):
                continue
        except (TypeError, ValueError):
            continue
        items[_pkey(it)] = it
    lines = []
    with _locked():
        st = _load()
        if st.get("opens") is None:
            # 首次运行：已有持仓只建基线，永不追溯触发（no retro-fire 的保证）
            st["opens"] = {k: {"inst_id": it["inst_id"], "side": _side(it), "status": "baseline",
                               "first_seen": settings.now_str()} for k, it in items.items()}
            _save(st)
            X.log_event("rr_recheck_baseline", count=len(items), keys=sorted(items))
            return [f"开仓后 RR 复核基线已建立：{len(items)} 个已有持仓只登记、不追溯"]
        opens = st["opens"]
        changed = False
        for k, it in items.items():
            if k not in opens:
                opens[k] = {"inst_id": it["inst_id"], "side": _side(it), "status": "pending",
                            "first_seen": settings.now_str(), "first_seen_ts": now_ts}
                changed = True
        mrr = min_rr(cfg)
        for k, rec in list(opens.items()):
            if rec.get("status") != "pending":
                continue
            it = items.get(k)
            if it is None:
                rec.update(status="gone", checked_at=settings.now_str())
                X.log_event("rr_recheck_skip", inst_id=rec["inst_id"], pos_key=k, reason="仓位已平，复核取消")
                changed = True
                continue
            sl, tp = (sltp or sltp_for)(it["inst_id"], it["pos_side"])
            try:
                fill = float(it["avg_px"] or 0)
            except (TypeError, ValueError):
                fill = 0.0
            if sl is None or tp is None or not fill:
                if now_ts - float(rec.get("first_seen_ts") or now_ts) >= GRACE_SECONDS:
                    rec.update(status="skipped", note="missing_sltp", fill=fill or None, sl=sl, tp=tp,
                               checked_at=settings.now_str())
                    X.log_event("rr_recheck_skip", inst_id=rec["inst_id"], pos_key=k, reason="止损或止盈缺失",
                                fill=fill or None, sl=sl, tp=tp)
                    lines.append(f"{rec['inst_id']} 开仓后 RR 复核跳过：止损/止盈缺失（sl={sl} tp={tp}）")
                    changed = True
                continue
            rr = sizing.rr_ratio(rec["side"], fill, sl, tp)
            rec.update(fill=fill, sl=sl, tp=tp, rr=rr, min_rr=mrr, checked_at=settings.now_str())
            changed = True
            if rr is not None and rr + 1e-12 >= mrr:
                rec["status"] = "ok"
                X.log_event("rr_recheck_ok", inst_id=rec["inst_id"], pos_key=k, fill=fill, sl=sl, tp=tp,
                            rr=round(rr, 4), min_rr=mrr)
                continue
            if halt:
                continue   # 只登记不触发：保持 pending，HALT 解除后补触发
            plan = (planned or planned_for)(rec["inst_id"], rec["side"], _reg_meta(rec["inst_id"]))
            detail = {"pos_key": k, "side": rec["side"], "fill": fill, "sl": sl, "tp": tp,
                      "sl_dist": abs(fill - sl), "tp_dist": abs(tp - fill), "rr": rr, "min_rr": mrr,
                      "planned_entry": plan.get("planned_entry"), "limit_px": plan.get("limit_px"),
                      "planned_sl": plan.get("planned_sl"), "planned_tp": plan.get("planned_tp"),
                      "planned_rr": plan.get("planned_rr"),
                      "from_algo": _reg_meta(rec["inst_id"]).get("from_algo")}
            rec["detail"] = detail
            res = (schedule or _schedule)(cfg, {"inst_id": rec["inst_id"], "trade_id": f"DEMO-{it['pos_id']}",
                                                "reason": "rr_recheck", "detail": detail})
            rec["schedule_result"] = res
            if res in ("已启动", "再讨论进行中", "该事件已讨论过"):
                rec["status"] = "fired"
                X.log_event("rr_recheck_fired", inst_id=rec["inst_id"], pos_key=k,
                            trade_id=f"DEMO-{it['pos_id']}", fill=fill, sl=sl, tp=tp,
                            rr=(round(rr, 4) if rr is not None else None), min_rr=mrr,
                            planned_entry=detail["planned_entry"], planned_rr=detail["planned_rr"])
                lines.append(f"{rec['inst_id']} 开仓后 RR 复核：实际 RR "
                             f"{('%.3f' % rr) if rr is not None else '无法计算（点位侧向无效）'} < {mrr:g} → 再讨论{res}")
            elif res == "关闭":
                rec["status"] = "skipped"
                rec["note"] = "rediscuss_disabled"
                X.log_event("rr_recheck_skip", inst_id=rec["inst_id"], pos_key=k, reason="再讨论总开关关闭")
            else:   # 启动失败等：保持 pending，下个巡检周期重试（schedule_rediscuss 内部已回滚在途/冷却）
                X.log_event("rr_recheck_spawn_failed", inst_id=rec["inst_id"], pos_key=k, result=str(res))
                lines.append(f"{rec['inst_id']} 开仓后 RR 复核：再讨论{res}，保持待触发，下轮重试")
        if changed:
            _save(st)
    return lines
