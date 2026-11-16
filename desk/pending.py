"""条件入场单（pending entry）：每币最多一张，新单替换旧单。

存储：state/ledger/pending_entries.json（当前挂单） + state/ledger/pending_events.jsonl（挂单/替换/撤销/触发/成交/拒单事件）。
本模块只管挂单的存取与事件记录；触发判定在模拟盘走 OKX 条件单（desk/entry_limit.py），不在本地。
（本地分钟线触发判定 find_trigger/expired 原供已退休的纸面监控 desk.monitor 使用，2026-11-15 删除。）"""
from __future__ import annotations

import datetime as _dt

from desk import settings

PENDING = settings.STATE / "ledger" / "pending_entries.json"
EVENTS = settings.STATE / "ledger" / "pending_events.jsonl"


def load() -> dict:
    return settings.read_json(PENDING, {})


def save(p: dict):
    settings.write_json(PENDING, p)


def event(kind: str, inst: str, **kw):
    rec = {"time": settings.now_str(), "date": settings.today(), "type": kind, "inst_id": inst, **kw}
    settings.append_jsonl(EVENTS, rec)
    return rec


def events(date: str | None = None) -> list[dict]:
    return [e for e in settings.read_jsonl(EVENTS) if date is None or e.get("date") == date]


def place(cfg, inst: str, order: dict) -> dict:
    p = load()
    now = settings.now()
    old = p.get(inst)
    order = {**order, "inst_id": inst, "status": "pending", "created": settings.now_str(),
             "created_ts_ms": int(now.timestamp() * 1000),
             "expires": (now + _dt.timedelta(hours=float(order.get("expire_hours") or 24))).strftime("%Y-%m-%d %H:%M:%S%z"),
             "id": f"PE-{now.strftime('%Y%m%d%H%M%S')}-{inst.split('-')[0]}"}
    if old:
        event("替换", inst, old_id=old["id"], new_id=order["id"])
    p[inst] = order
    save(p)
    event("挂单", inst, id=order["id"], side=order.get("side", "long"), leverage=order.get("leverage", 1), trigger_px=order["trigger_px"], condition=order["condition"],
          stop_loss=order["stop_loss"], size_pct=order["size_pct"], decision_id=order.get("decision_id"),
          source=order.get("source"), role_levels=order.get("role_levels"))
    return order


def cancel(cfg, inst: str, reason: str):
    p = load()
    if inst in p:
        o = p.pop(inst)
        save(p)
        event("撤销", inst, id=o["id"], reason=reason)
