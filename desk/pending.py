"""条件入场单（pending entry）：每币最多一张，新单替换旧单。只做纸面成交。

存储：state/ledger/pending_entries.json（当前挂单） + state/ledger/pending_events.jsonl（挂单/替换/撤销/触发/成交/拒单事件）。"""
from __future__ import annotations

import datetime as _dt

import pandas as pd

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


def expired(o: dict, now: _dt.datetime) -> bool:
    return now >= _dt.datetime.strptime(o["expires"], "%Y-%m-%d %H:%M:%S%z")


def find_trigger(o: dict, bars: pd.DataFrame, near_pct: float = 0.0):
    """在 o['last_check_ts_ms'] 之后的已收盘分钟K线里找首次触发。返回 (bar_ts, 成交参考价, 是否真正触及) 或 None。
    gte：High≥触发价；跳空高开（Open>触发价）按开盘价。lte：Low≤触发价；跳空低开按开盘价。
    near_pct>0 时（confirm 模式）价格进入触发价 near_pct% 以内也算"接近触发"。"""
    since = o.get("last_check_ts_ms") or o["created_ts_ms"]
    t = float(o["trigger_px"])
    for b in bars[bars.ts >= since].itertuples():
        if o["condition"] == "gte":
            if b.High >= t:
                return int(b.ts), (b.Open if b.Open > t else t), True
            if near_pct and b.High >= t * (1 - near_pct / 100):
                return int(b.ts), float(b.Close), False
        else:
            if b.Low <= t:
                return int(b.ts), (b.Open if b.Open < t else t), True
            if near_pct and b.Low <= t * (1 + near_pct / 100):
                return int(b.ts), float(b.Close), False
    return None
