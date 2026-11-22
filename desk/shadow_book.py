"""反向影子账（2026-11-22 审查共识包 D 项）：纸面记账，零下单、零 LLM 调用。

目的：交易经理按设计无权重、不能翻转共识，它对共识方向的「不认可」只表现为否决。
这本影子账专门测量它的反共识直觉有没有 edge：命中「TM 反向事件」时，按 TM 的隐含
反方向、用基线构造开一笔纸面仓，到期按 1H 先触口径结算 R，只记账、永不下单。

事件定义（以决策记录里真实存在的字段为准，识别不了的不硬凑）：
1. 结构型（最强信号）：rec.tm_flip_blocked == True——交易经理的立场方向曾与共识相反、
   被方向门按否决处理；隐含方向 = 其立场方向。历史 218 条决策中此型为 0（TM 从未真试图翻转）。
2. 文本型：rec.tm_vetoed_consensus == True（共识有方向、交易经理否决）且交易经理理由
   （manager.rationale）中出现闭集内的**反向价格预期**标记词（按共识方向取反向词表，
   见 COUNTER_MARKERS）。命中词逐笔入账备查。这是文本启发式，不是 TM 的结构化反向投票
   ——TM 按设计没有这个字段；词表与口径如需调整，先改本文件并在日报/报告里说明。

基线构造（与否决可成交性回放同口径）：入场 = 决策时现价；止损 = 1.5×ATR14（决策记录
market.atr14）；止盈 = RR 2.0（risk.tp_r_multiple）；期限 5 天；结算 = 1H 已收盘 K 线
先触判定、同根 K 止损优先（复用 gate_ledger._path_r，不另写一套）。

账本：state/ledger/shadow_book.jsonl，事件溯源两类行（open / settle），以 decision_id
幂等；run_tail 每场收尾扫描新决策开仓 + 结算到期仓，历史回溯补记走同一函数。
"""
from __future__ import annotations

import datetime as _dt

from desk import settings

LEDGER = settings.STATE / "ledger" / "shadow_book.jsonl"
HORIZON_DAYS = 5

# 共识方向 → TM 理由里表示「预期反向运动」的标记词（闭集）。
# 共识做空时，TM 若预期反弹/轧空，即隐含看多；共识做多时镜像。
COUNTER_MARKERS = {
    "short": ("反弹", "轧空", "挤压", "逼空", "空头回补", "止跌", "回升", "超卖",
              "正背离", "长下影", "买盘恢复", "买盘回升", "动能修复", "反转向上"),
    "long": ("回落", "见顶", "冲高回落", "超买", "负背离", "长上影", "卖盘增强", "转跌", "假突破"),
}
_OPPOSITE = {"long": "short", "short": "long"}


def HOURLY(inst):   # 可在测试中替换（1H 已收盘 K 线）
    from desk import okx_data
    return okx_data.candles(inst, "1H", 1500)


def identify(rec: dict) -> dict | None:
    """决策记录 → TM 反向事件（或 None）。纯函数，只读记录字段。"""
    cs = rec.get("consensus_side")
    if cs not in ("long", "short"):
        return None
    if rec.get("tm_flip_blocked"):
        st = (rec.get("stances") or {}).get("trade_manager") or {}
        d = st.get("direction")
        shadow = "long" if d == "bullish" else ("short" if d == "bearish" else _OPPOSITE[cs])
        return {"kind": "flip_blocked", "shadow_side": shadow, "markers": ["tm_flip_blocked"]}
    if not rec.get("tm_vetoed_consensus"):
        return None
    text = ((rec.get("manager") or {}).get("rationale") or "")
    hits = [m for m in COUNTER_MARKERS[cs] if m in text]
    if not hits:
        return None
    return {"kind": "veto_counter_text", "shadow_side": _OPPOSITE[cs], "markers": hits}


def _open_row(cfg, rec: dict, ev: dict) -> dict | None:
    mk = rec.get("market") or {}
    px, atr = float(rec.get("price") or 0), float(mk.get("atr14") or 0)
    if px <= 0 or atr <= 0:
        return None
    from desk import sizing
    mult = float(sizing.rcfg(cfg).get("tp_r_multiple", 2.0))
    stop_atr = float(sizing.rcfg(cfg).get("stop_atr_mult", 1.5))
    sgn = 1 if ev["shadow_side"] == "long" else -1
    dist = stop_atr * atr
    return {"kind": "open", "decision_id": rec["decision_id"], "date": rec.get("date"),
            "time": rec.get("time"), "session": rec.get("session"), "inst": rec.get("inst_id"),
            "consensus_side": rec.get("consensus_side"), "shadow_side": ev["shadow_side"],
            "event_kind": ev["kind"], "markers": ev["markers"],
            "entry": px, "sl": px - sgn * dist, "tp": px + sgn * dist * mult,
            "atr14": atr, "horizon_days": HORIZON_DAYS}


def _merged() -> dict:
    out = {}
    for r in settings.read_jsonl(LEDGER):
        did = r.get("decision_id")
        if not did:
            continue
        if r.get("kind") == "open":
            out[did] = {**r, "status": "open"}
        elif r.get("kind") == "settle" and did in out:
            out[did].update(status="settled", exit=r.get("exit"), pnl_r=r.get("pnl_r"),
                            settled_at=r.get("settled_at"))
    return out


def _settle_one(row: dict, df) -> dict | None:
    """窗口已完整走完才结算；数据不足（窗口未走完/取不到线）返回 None 保持 open。"""
    from desk import gate_ledger
    import pandas as pd
    t0 = gate_ledger._t0_utc(row["time"])
    end = t0 + _dt.timedelta(hours=24 * int(row.get("horizon_days") or HORIZON_DAYS))
    win = df[(df.Date >= pd.Timestamp(t0)) & (df.Date < pd.Timestamp(end))]
    if win.empty or win.Date.iloc[-1] < pd.Timestamp(end) - _dt.timedelta(hours=1):
        return None
    bars = [(float(r.High), float(r.Low), float(r.Close)) for r in win.itertuples()]
    sgn = 1 if row["shadow_side"] == "long" else -1
    pnl = gate_ledger._path_r(bars, float(row["entry"]), float(row["sl"]), float(row["tp"]), sgn)
    if pnl is None:
        return None
    rr = abs(float(row["tp"]) - float(row["entry"])) / abs(float(row["entry"]) - float(row["sl"]))
    exit_kind = "tp" if pnl >= rr - 1e-9 else ("sl" if pnl <= -1.0 + 1e-9 else "time")
    return {"kind": "settle", "decision_id": row["decision_id"], "exit": exit_kind,
            "pnl_r": pnl, "settled_at": settings.now_str()}


def run_tail(cfg, now=None) -> dict:
    """扫描全部决策记录：新命中的开影子仓、到期的结算。幂等（decision_id 已在账不重开/不重结）。
    返回 {opened, settled} 计数。历史回溯补记走同一入口（同一识别与结算口径）。"""
    from desk import manager
    now = now or settings.now()
    now_naive = now.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    book = _merged()
    opened = settled = 0
    cache: dict = {}
    for rec in settings.read_jsonl(manager.DECISIONS):
        did = rec.get("decision_id")
        if not did:
            continue
        row = book.get(did)
        if row is None:
            ev = identify(rec)
            if ev is None:
                continue
            row = _open_row(cfg, rec, ev)
            if row is None:
                continue
            settings.append_jsonl(LEDGER, row)
            row = {**row, "status": "open"}
            book[did] = row
            opened += 1
        if row.get("status") == "open":
            from desk import gate_ledger
            try:
                t0 = gate_ledger._t0_utc(row["time"])
            except Exception:
                continue
            if now_naive < t0 + _dt.timedelta(hours=24 * int(row.get("horizon_days") or HORIZON_DAYS)):
                continue
            inst = row["inst"]
            if inst not in cache:
                try:
                    cache[inst] = HOURLY(inst)
                except Exception:
                    cache[inst] = None
            if cache[inst] is None:
                continue
            s = _settle_one(row, cache[inst])
            if s is not None:
                settings.append_jsonl(LEDGER, s)
                settled += 1
    return {"opened": opened, "settled": settled}


def report_lines(cfg) -> list[str]:
    """日报一行：影子账累计战绩（无任何影子仓时不出现）。"""
    book = _merged()
    if not book:
        return []
    settled = [r for r in book.values() if r.get("status") == "settled"]
    open_n = sum(1 for r in book.values() if r.get("status") == "open")
    if not settled:
        return [f"反向影子账：已开 {len(book)} 笔纸面反向仓，尚无到期结算（仅记账，不下单）"]
    wins = sum(1 for r in settled if (r.get("pnl_r") or 0) > 0)
    tot = sum(float(r.get("pnl_r") or 0) for r in settled)
    return [f"反向影子账：累计 {len(book)} 笔（已结算 {len(settled)}：赢 {wins} / 亏 {len(settled) - wins}，"
            f"合计 {tot:+.2f} R；未结算 {open_n}）——交易经理否决共识时按其隐含反向开的纸面仓，仅记账不下单"]


if __name__ == "__main__":
    cfg = settings.load_config()
    print(run_tail(cfg))
