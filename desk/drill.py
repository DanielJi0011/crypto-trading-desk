"""演练标记：state/drill.json = {"insts": ["DOGE-USDT-SWAP"], "until": epoch秒}。
标记期内这些合约的 demo_ws 通知加【演练】前缀，止损后不启动全角色再讨论（冷却照常记录）。过期自动失效；演练结束请删除该文件。"""
from __future__ import annotations

import time

from desk import settings

PATH = settings.STATE / "drill.json"


def active(inst_id: str | None, now: float | None = None) -> bool:
    if not inst_id:
        return False
    d = settings.read_json(PATH, {})
    return inst_id in (d.get("insts") or []) and float(now or time.time()) < float(d.get("until") or 0)
