"""2026-10-30 方案一 B：交易经理账本。每笔平仓的盈亏记到交易经理名下（交易经理是最终定点位的人）。

文件：state/ledger/tm_ledger.jsonl，一笔平仓一行，按 trade_id 幂等（重复写入直接跳过）。
字段：trade_id、inst_id、side、开仓决策号 open_decision_id、entry/exit 价与时间、已实现盈亏 pnl_usd、收益率 pnl_pct、
实际盈亏比 realized_rr（(出场−入场)·方向 / |入场−止损|，止损缺失为 None）、持有天数 hold_days、
是否否决过共识 vetoed_consensus（开仓决策本身或持仓期间同币任一决策带 tm_vetoed_consensus）、exit_reason、decided_by。
只读写本地文件，不下单、不读密钥。"""
from __future__ import annotations

from datetime import datetime

from desk import settings

PATH = settings.STATE / "ledger" / "tm_ledger.jsonl"
DECISIONS = settings.STATE / "decisions.jsonl"
SCORES = settings.STATE / "scores.jsonl"


def enabled(cfg) -> bool:
    return bool(((cfg or {}).get("ledger") or {}).get("tm_attribution", True))


def _t(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "")[:19].replace("T", " "))
    except (TypeError, ValueError):
        return None


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def realized_rr(trade) -> float | None:
    e, x, sl = _f(trade.get("entry_px")), _f(trade.get("exit_px")), _f(trade.get("sl_px"))
    if not e or x is None or not sl or abs(e - sl) < 1e-12:
        return None
    sg = -1 if str(trade.get("side")) in ("sell", "short") else 1
    return round(sg * (x - e) / abs(e - sl), 3)


def _vetoed(trade, decs) -> bool:
    did, inst = trade.get("signal_id"), trade.get("inst_id")
    t0, t1 = _t(trade.get("entry_time")), _t(trade.get("exit_time"))
    for d in decs:
        if not d.get("tm_vetoed_consensus"):
            continue
        if d.get("decision_id") == did:
            return True
        td = _t(d.get("time"))
        if d.get("inst_id") == inst and td and t0 and t1 and t0 <= td <= t1:
            return True
    return False


def entry_of(trade, decs=None) -> dict:
    decs = settings.read_jsonl(DECISIONS) if decs is None else decs
    t0, t1 = _t(trade.get("entry_time")), _t(trade.get("exit_time"))
    hd = round((t1 - t0).total_seconds() / 86400, 2) if t0 and t1 else None
    return {"trade_id": trade["trade_id"], "inst_id": trade.get("inst_id"), "side": trade.get("side"),
            "open_decision_id": trade.get("signal_id"), "entry_px": _f(trade.get("entry_px")), "exit_px": _f(trade.get("exit_px")),
            "entry_time": trade.get("entry_time"), "exit_time": trade.get("exit_time"), "sl_px": _f(trade.get("sl_px")),
            "tp_px": _f(trade.get("tp_px")), "pnl_usd": _f(trade.get("pnl_usd")) or 0.0, "pnl_pct": _f(trade.get("pnl_pct")),
            "realized_rr": realized_rr(trade), "hold_days": hd, "vetoed_consensus": _vetoed(trade, decs),
            "exit_reason": trade.get("exit_reason"), "decided_by": "trade_manager", "recorded_at": settings.now_str()}


def read() -> list[dict]:
    return settings.read_jsonl(PATH) if PATH.exists() else []


def record_closed(cfg, trades) -> list[dict]:
    """写入新平仓（已存在的 trade_id 跳过，保证幂等）。返回本次新写入的行。"""
    if not enabled(cfg):
        return []
    seen = {x.get("trade_id") for x in read()}
    decs, out = None, []
    for t in trades or []:
        if t.get("status", "closed") != "closed" or not t.get("trade_id") or t["trade_id"] in seen or t.get("exit_px") in (None, ""):
            continue
        decs = settings.read_jsonl(DECISIONS) if decs is None else decs
        row = entry_of(t, decs)
        PATH.parent.mkdir(parents=True, exist_ok=True)
        settings.append_jsonl(PATH, row)
        seen.add(t["trade_id"]); out.append(row)
    return out


def stats(rows=None) -> dict:
    rows = read() if rows is None else rows
    n = len(rows)
    rr = [r["realized_rr"] for r in rows if r.get("realized_rr") is not None]
    return {"n": n, "pnl": round(sum(float(r.get("pnl_usd") or 0) for r in rows), 2),
            "win_rate": (sum(1 for r in rows if float(r.get("pnl_usd") or 0) > 0) / n) if n else None,
            "avg_rr": (sum(rr) / len(rr)) if rr else None, "rr_n": len(rr)}


_VETO_MIN_ROLES = 3    # 定案门槛：同一周期至少 3 个不同角色有到期打分
_VETO_TARGET_H = 5     # 定案周期：离 5 天最近者优先（台子的主打分周期）


def _veto_outcome(ss):
    """一组同决策的打分 → 定案用的事后价格结果（涨/跌/平）。

    口径（2026-11-09 用户批准修改）：旧口径取排序后的第一条打分定案，实际等于由最先到期
    的单个角色（多为 flows）说了算，样本单薄且口径任意。新口径：按周期分组，同一周期内
    每个角色只取最新一条打分；只有角色数 ≥3 的周期才有资格定案，选离 5 天最近的周期
    （并列时角色多者优先、再并列时周期长者优先）；该周期各角色 outcome 取中位数
    （跌=-1、平=0、涨=1，偶数个取中间两值均值，均值落在 ±1 之内记平）。
    无周期达标 → 返回 None（判定为“尚无结果”，等打分攒够再判，不硬判）。"""
    by_h = {}
    for s in ss:
        if s.get("outcome") not in ("涨", "跌", "平"):
            continue
        slot = by_h.setdefault(s.get("horizon"), {})
        prev = slot.get(s.get("role"))
        if prev is None or str(s.get("scored_at") or "") >= str(prev.get("scored_at") or ""):
            slot[s.get("role")] = s
    cand = [(h, m) for h, m in by_h.items() if len(m) >= _VETO_MIN_ROLES]
    if not cand:
        return None

    def keyf(item):
        h, m = item
        hv = _VETO_TARGET_H if h is None else h
        return (abs(hv - _VETO_TARGET_H), -len(m), -hv)

    h, m = min(cand, key=keyf)
    vals = sorted({"跌": -1, "平": 0, "涨": 1}[s["outcome"]] for s in m.values())
    n = len(vals)
    med = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
    return "涨" if med >= 1 else ("跌" if med <= -1 else "平")


def veto_stats(decs=None, scores=None) -> dict:
    """交易经理否决共识后的结果：用该决策的打分 outcome 判断共识方向事后是否落空（定案口径见 _veto_outcome）。"""
    decs = settings.read_jsonl(DECISIONS) if decs is None else decs
    scores = (settings.read_jsonl(SCORES) if SCORES.exists() else []) if scores is None else scores
    vetoes = [d for d in decs if d.get("tm_vetoed_consensus")]
    by = {}
    for s in scores:
        by.setdefault(s.get("decision_id"), []).append(s)
    right = wrong = 0
    for d in vetoes:
        side = d.get("consensus_side")
        if not side:
            continue
        outcome = _veto_outcome(by.get(d.get("decision_id")) or [])
        if outcome is None:
            continue
        if outcome == ("涨" if side == "long" else "跌"):
            wrong += 1     # 共识方向事后对了 → 否决错过行情
        else:
            right += 1     # 共识方向事后落空 → 否决避开亏损
    return {"n": len(vetoes), "judged": right + wrong, "veto_right": right, "veto_wrong": wrong}


def report_lines(cfg) -> list[str]:
    if not enabled(cfg):
        return []
    s, v = stats(), veto_stats()
    L = ["## 三·五、交易经理战绩（每一单由交易经理定点位，盈亏记在交易经理名下）", ""]
    if not s["n"]:
        L.append("- 累计盈亏、胜率、平均盈亏比、笔数：暂无数据（账本还没有平仓记录）")
    else:
        L += [f"- 累计已实现盈亏：{s['pnl']:+.2f} USDT；笔数：{s['n']}",
              f"- 胜率：{s['win_rate']:.1%}" if s["win_rate"] is not None else "- 胜率：暂无数据",
              f"- 平均实际盈亏比：{s['avg_rr']:+.2f}（{s['rr_n']} 笔有止损可算）" if s["avg_rr"] is not None else "- 平均实际盈亏比：暂无数据"]
    if not v["n"]:
        L.append("- 否决共识后的结果：暂无数据（还没有否决共识的记录）")
    elif not v["judged"]:
        L.append(f"- 否决共识 {v['n']} 次；事后结果：暂无数据（打分尚未到期）")
    else:
        L.append(f"- 否决共识 {v['n']} 次，已有结果 {v['judged']} 次：否决正确（共识方向落空）{v['veto_right']} 次，"
                 f"否决错过行情（共识方向事后正确）{v['veto_wrong']} 次")
    L.append("")
    return L
