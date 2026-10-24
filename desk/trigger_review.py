"""R10（第一批）：风险状态变化时撤销入场类挂单。

触发条件（任一）：KILL（state/KILL 或旧路径 state/ledger/KILL）、HALT（state/HALT）、当日亏损 daily_loss_halt、回撤 drawdown_kill（R5）。
撤销范围：
  1) 入场类条件单：/trade/orders-algo-pending ordType=trigger 且为开仓方向（含 protected_algo_ids 受保护单，紧急停机不例外）；
  2) 在途入场子单：/trade/orders-pending 中开仓方向、非 reduceOnly 的普通挂单（条件单触发后的限价子单、补单）。
绝不撤：reduceOnly 单、平仓方向的单、conditional/oco 止损止盈算法单（本模块不查询也不撤这两类）。
撤单后查询子单终态与成交量并记事件（trigger_revoked / entry_child_revoked），已成交部分由交易所附带止损与 entry_limit 保护流程覆盖。
调用方：demo_watch 每轮开头（兜底）；demo_ws 进程内每 30 秒检查一次（需重启 demo_ws 才生效）。
HALT 下撤单经 okx_demo.post(allow_halt=True) 放行（R7）；这是 HALT 下唯一允许的自动写操作。
!! 禁止实盘：只经 desk/okx_demo.py（OKX_DEMO_* + x-simulated-trading: 1）。不下单。
"""
from __future__ import annotations

from desk import okx_demo as X
from desk import settings


def _true(x) -> bool:
    return str(x).lower() == "true"


def is_entry(o: dict) -> bool:
    """开仓方向：双向持仓 long+buy / short+sell；单向持仓 net 时以非 reduceOnly 视为开仓。reduceOnly 一律不是入场。"""
    if _true(o.get("reduceOnly")):
        return False
    ps, sd = o.get("posSide") or "net", o.get("side")
    if ps == "long":
        return sd == "buy"
    if ps == "short":
        return sd == "sell"
    return True


def emergency_reasons(cfg, guard_status: dict | None = None, notifier=None) -> list[str]:
    out = []
    if settings.kill_active():
        out.append("kill")
    if settings.halt_active():
        out.append("halt")
    st = guard_status
    if st is None:
        from desk import equity_guard
        st = equity_guard.status(cfg)
        if not st.get("unavailable"):
            equity_guard.apply_breach(cfg, st, notifier)
            if st.get("drawdown_kill") and "kill" not in out:   # apply_breach 刚写了 KILL
                out.append("kill")
    if st.get("daily_halt"):
        out.append("daily_loss_halt")
    if st.get("drawdown_kill"):
        out.append("drawdown_kill")
    return out


def _order_state(inst, ord_id) -> dict:
    try:
        d = X.get("/api/v5/trade/order", instId=inst, ordId=str(ord_id)).get("data") or []
        return d[0] if d else {}
    except Exception as e:
        return {"state": f"query_failed:{type(e).__name__}"}


def revoke_entries(cfg, why: str, notifier=None) -> dict:
    """撤所有入场类条件单与在途入场子单。返回 {algos: [...], orders: [...], errors: [...]}。"""
    from desk import demo_exec as DX
    prot = {str(x) for x in DX.ecfg(cfg)["protected_algo_ids"]}
    out = {"algos": [], "orders": [], "errors": []}
    algos = X.get("/api/v5/trade/orders-algo-pending", ordType="trigger", instType="SWAP").get("data", [])
    for a in algos:
        if not is_entry(a):
            continue
        aid = str(a["algoId"])
        try:
            X.post("/api/v5/trade/cancel-algos", [{"instId": a["instId"], "algoId": aid}], allow_halt=True)
            r = DX.reg(); r["triggers"].pop(aid, None); DX.save_reg(r)
            X.log_event("trigger_revoked", inst_id=a["instId"], algoId=aid, why=why, protected=aid in prot)
            out["algos"].append(aid)
        except Exception as e:
            X.log_event("trigger_revoke_failed", inst_id=a.get("instId"), algoId=aid, why=why, error=f"{type(e).__name__}: {e}"[:300])
            out["errors"].append(aid)
    orders = X.get("/api/v5/trade/orders-pending", instType="SWAP").get("data", [])
    for o in orders:
        if not is_entry(o):
            continue
        oid = str(o["ordId"])
        try:
            X.post("/api/v5/trade/cancel-order", {"instId": o["instId"], "ordId": oid}, allow_halt=True)
            fin = _order_state(o["instId"], oid)   # 撤单与成交竞态：以查询到的终态与成交量为准
            X.log_event("entry_child_revoked", inst_id=o["instId"], ordId=oid, why=why, state=fin.get("state"),
                        accFillSz=fin.get("accFillSz"), sz=o.get("sz"))
            out["orders"].append(oid)
        except Exception as e:
            X.log_event("entry_child_revoke_failed", inst_id=o.get("instId"), ordId=oid, why=why, error=f"{type(e).__name__}: {e}"[:300])
            out["errors"].append(oid)
    if out["algos"] or out["orders"] or out["errors"]:
        try:
            if notifier is None:
                from desk.demo_ws import notify as notifier
            notifier({"kind": "trigger_revoked", "message": f"风险状态 {why}：撤入场条件单 {len(out['algos'])} 张、在途入场子单 "
                                                             f"{len(out['orders'])} 张，失败 {len(out['errors'])}；止损止盈单未动"})
        except Exception as e:
            print(f"[trigger_review] 通知失败 {type(e).__name__}", flush=True)
    return out


def poll(cfg, notifier=None) -> dict:
    """检查风险状态，需要时撤销。返回 {reasons, ...revoke 结果}；无触发返回 {reasons: []}。"""
    reasons = emergency_reasons(cfg, notifier=notifier)
    if not reasons:
        return {"reasons": []}
    res = revoke_entries(cfg, ",".join(reasons), notifier=notifier)
    return {"reasons": reasons, **res}
