"""L6：止损记录与披露。止损触发时记下币种与被止损仓位的方向（demo_ws/demo_watch 从共享模拟账户的成交回补记录，
无法区分是哪台机器/哪次决策下的单）。
记录文件 state/stop_cooldown.json：{inst_id: {"side": "long"|"short", "ts": epoch秒, "algoId": ...}}。

2026-11-05 用户决定：取消 24 小时同向硬冷却（risk.stop_cooldown_hours=0，blocked() 恒为 None），改信息披露制——
最近一次止损通过 describe() 写进交易经理提示词，是否止损后同向重开由交易经理知情后自行决定（见 manager._prompt）。
blocked() 与 _cooldown()/订单工具的 [COOLDOWN] 拦截保留为可配置机制，stop_cooldown_hours>0 时恢复硬拦截。"""
from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

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
    from desk import excluded_trades
    if algo_id is not None and excluded_trades.is_excluded_algo(algo_id):
        return None           # 用户排除的交易的止损不记录、不披露
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


def last_record(inst_id: str, now: float | None = None) -> dict | None:
    """最近一次止损记录（不看冷却窗口）：{side, ts, algoId, age_h}；无记录返回 None。"""
    r = settings.read_json(PATH, {}).get(inst_id)
    if not r or not r.get("side"):
        return None
    ts = float(r["ts"])
    age = (float(now if now is not None else time.time()) - ts) / 3600
    return {"side": r["side"], "ts": ts, "algoId": r.get("algoId"), "age_h": round(age, 2)}


def describe(inst_id: str, consensus_side: str | None = None, now: float | None = None) -> str | None:
    """止损披露文本（披露制）：写进交易经理提示词，让它知道最近一次止损的背景后再自行决定是否重开。
    无记录返回 None。consensus_side 为当前共识方向（long/short），与止损同向时点明“止损后同向重开”。"""
    r = last_record(inst_id, now=now)
    if not r:
        return None
    when = datetime.fromtimestamp(r["ts"], ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")
    side_cn = "多" if r["side"] == "long" else "空"
    lines = [
        f"本币最近一次止损：{side_cn}单于北京时间 {when} 被止损（约 {r['age_h']:.1f} 小时前，止损单 algoId {r.get('algoId')}）。",
        "注意：这条记录来自共享模拟账户的成交回补，无法区分是哪台机器、哪次决策下的单，不能默认是本台判断失误。",
    ]
    if consensus_side and consensus_side == r["side"]:
        lines.append(
            f"当前共识方向同为{side_cn}：若本次开仓，即属于止损后同向重开。冷却硬拦截已取消（2026-11-05 用户决定），"
            "是否重开由你自行决定；决定重开时必须在理由中写明这笔止损背景以及这次与上次的不同，不重开也请在理由中说明。"
        )
    return "\n".join(lines)
