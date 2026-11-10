"""排除交易（2026-11-09 用户指示）：经确认不属于本台决策的成交（如另一台机器误下的单），
从平仓复盘、交易经理账本、止损记录中排除。

排除清单 state/ledger/excluded_trades.json（人工维护，不写死在代码里）：
  {"DEMO-<posId>-<cTime>": {"reason": "...", "algo_id": "<关联止损单 algoId，可选>"}}
各写入点在记录前查表，命中即跳过；已写入的行由各模块在下次同步时清掉（见 demo_trades.drop_excluded）。
"""
from __future__ import annotations

from desk import settings

PATH = settings.STATE / "ledger" / "excluded_trades.json"


def load() -> dict:
    try:
        return settings.read_json(PATH, {}) or {}
    except Exception:
        return {}


def is_excluded(trade_id) -> bool:
    return bool(trade_id) and str(trade_id) in load()


def is_excluded_algo(algo_id) -> bool:
    if not algo_id:
        return False
    return any(str(v.get("algo_id")) == str(algo_id) for v in load().values() if isinstance(v, dict))
