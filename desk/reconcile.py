"""R11.3 下单意图对账（demo_watch 每轮调用）。

okx_demo.post 对带 clOrdId/algoClOrdId 的下单写 state/intents.jsonl：intent → ack 或 error。
只有 intent 没有 ack（进程中断、超时）或以 error 结束的记录，结果不明：按 clOrdId 向交易所查询。
1. 查到 → 记 intent_found；该单是“调用方以为失败但其实已下”的单，发 intent_orphan_found 告警，由人工或后续流程处理，不自动撤。
2. 查不到（51603 或空）→ 记 intent_absent，确认没有下出去。
3. 查询失败 → 本轮不处理，下轮再查。
只查最近 MAX_AGE_H 小时内、且已过 GRACE_S 秒的记录（避免与正在进行的下单竞争）。
"""
from __future__ import annotations

import datetime as _dt

from desk import okx_demo as X
from desk import settings

GRACE_S = 60
MAX_AGE_H = 24
FINAL = ("ack", "found", "absent")


def _t(s):
    return _dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S%z")


def pending(now=None) -> list[dict]:
    now = now or settings.now()
    last = {}
    for r in settings.read_jsonl(X.INTENTS):
        last[r["cl"]] = r
    out = []
    for cl, r in last.items():
        if r["state"] in FINAL:
            continue
        age = (now - _t(r["time"])).total_seconds()
        if GRACE_S <= age <= MAX_AGE_H * 3600:
            out.append(r)
    return out


def _lookup(r):
    try:
        if r["path"].endswith("order-algo"):
            d = X.get("/api/v5/trade/order-algo", algoClOrdId=r["cl"]).get("data") or []
        else:
            d = X.get("/api/v5/trade/order", instId=r["inst_id"], clOrdId=r["cl"]).get("data") or []
    except X.DemoError as e:
        if "51603" in str(e):
            return "absent", None
        raise
    return ("found", d[0]) if d else ("absent", None)


def run(now=None) -> list:
    from desk import demo_exec as DX
    out = []
    for r in pending(now):
        try:
            st, d = _lookup(r)
        except Exception as e:
            out.append((r["cl"], "unknown")); X.log_event("intent_lookup_failed", cl=r["cl"], error=type(e).__name__); continue
        rec = {k: r.get(k) for k in ("cl", "path", "inst_id", "ordType", "sz", "side", "reduceOnly", "source")}
        if st == "found":
            ids = {"ordId": d.get("ordId"), "algoId": d.get("algoId"), "exch_state": d.get("state")}
            settings.append_jsonl(X.INTENTS, {"time": settings.now_str(), **rec, "state": "found", **ids})
            DX._alert("intent_orphan_found", inst_id=r["inst_id"], cl=r["cl"], **ids,
                      message=f"{r['inst_id']} 下单 {r['cl']} 本地记为未确认，交易所查到该单（{d.get('state')}），请核对")
        else:
            settings.append_jsonl(X.INTENTS, {"time": settings.now_str(), **rec, "state": "absent"})
            X.log_event("intent_absent", inst_id=r["inst_id"], cl=r["cl"])
        out.append((r["cl"], st))
    return out
