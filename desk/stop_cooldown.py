"""L6：止损后同向冷却。止损触发时记下币种与被止损仓位的方向；冷却期内同币同向不开新仓、不挂新条件单（事件 cooldown_blocked）。
记录文件 state/stop_cooldown.json：{inst_id: {"side": "long"|"short", "ts": epoch秒, "algoId": ...}}。反向开仓不受限。"""
from __future__ import annotations

import time

from desk import settings

PATH = settings.STATE / "stop_cooldown.json"


def hours(cfg) -> float:
    return float((cfg.get("risk") or {}).get("stop_cooldown_hours", 0) or 0)


def _dir(side: str | None, pos_side: str | None) -> str | None:
    """被止损仓位的方向：双向持仓看 posSide；单向（net）时止损单 sell=平多、buy=平空。"""
    if pos_side in ("long", "short"):
        return pos_side
    return {"sell": "long", "buy": "short"}.get(side or "")


def record(inst_id: str, side: str | None, pos_side: str | None = None, algo_id=None, now: float | None = None) -> str | None:
    d = _dir(side, pos_side)
    if not inst_id or not d:
        return None
    m = settings.read_json(PATH, {})
    m[inst_id] = {"side": d, "ts": float(now if now is not None else time.time()), "algoId": algo_id}
    settings.write_json(PATH, m)
    return d


def blocked(cfg, inst_id: str, side: str, now: float | None = None) -> dict | None:
    """冷却中返回 {side, since_h, left_h}；否则 None。"""
    h = hours(cfg)
    if h <= 0:
        return None
    r = settings.read_json(PATH, {}).get(inst_id)
    if not r or r.get("side") != side:
        return None
    age = (float(now if now is not None else time.time()) - float(r["ts"])) / 3600
    return {"side": side, "since_h": round(age, 2), "left_h": round(h - age, 2)} if age < h else None
