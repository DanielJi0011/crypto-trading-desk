"""OKX 模拟盘已平仓交易 → 复盘用 trade 结构（只读，不下单、不撤单）。

数据源（按可靠性）：
  1) OKX 模拟盘 GET /api/v5/account/positions-history（经 desk.okx_demo，强制 x-simulated-trading: 1，只读）
     —— 开/平均价、张数、已实现盈亏（含手续费/资金费）、开平时间、平仓类型。
  2) 本地模拟盘记录关联 decision_id：state/ledger/demo_orders.json（registry）、demo_events.jsonl
     （trigger_placed/trigger_filled/open_ok/eth_backfill_ok）、旧纸面账本 trades.csv（含归档，补单仓沿用其 signal_id）、
     demo_ws_events.jsonl（止损/止盈触发）。
  3) 转换结果缓存到 state/ledger/demo_closed.json；API 不可用时用缓存（离线也能复盘已见过的平仓）。
trade_id = DEMO-<posId>-<cTime>：OKX 会复用 posId（同一 posSide 多次开平），加开仓毫秒时间戳保证稳定唯一。
"""
from __future__ import annotations

import csv
import datetime as _dt

from desk import settings

CACHE = settings.STATE / "ledger" / "demo_closed.json"
ARCHIVE_TRADES = settings.STATE / "archive"
WIN_MS = 180_000           # 本地事件与交易所时间的匹配窗口
TEST_EVENTS = ("test_open", "test_close", "test_market", "test_trigger")
CLOSE_TYPE = {"1": "部分平仓", "2": "平仓", "3": "强平", "4": "部分强平", "5": "自动减仓(ADL)"}


def _ms(s) -> int | None:
    try:
        return int(_dt.datetime.strptime(str(s)[:24].strip(), "%Y-%m-%d %H:%M:%S%z").timestamp() * 1000)
    except ValueError:
        try:
            return int(_dt.datetime.strptime(str(s).strip(), "%Y-%m-%d %H:%M%z").timestamp() * 1000)
        except ValueError:
            return None


def _ts(ms) -> str:
    return _dt.datetime.fromtimestamp(int(ms) / 1000, settings.TZ).strftime("%Y-%m-%d %H:%M:%S%z")


def _near(t, ms, win=WIN_MS) -> bool:
    m = _ms(t)
    return m is not None and abs(m - int(ms)) <= win


def paper_trades() -> list[dict]:
    """本地纸面账本 + 归档账本里的全部交易（只读）。"""
    out = []
    try:
        from desk import ledger
        out += ledger.store.read_trades()
    except Exception:
        pass
    for p in sorted(ARCHIVE_TRADES.glob("paper-ledger-*/journal/trades.csv")) if ARCHIVE_TRADES.exists() else []:
        with p.open(encoding="utf-8") as f:
            out += list(csv.DictReader(f))
    return out


def _f(x, d=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def link(row: dict, events: list[dict], reg: dict, papers: list[dict]) -> dict:
    """为一条 positions-history 找本地决策 id / 止损止盈 / 入场理由。返回 {decision_id, sl, tp, reason, source, test}。"""
    inst, c = row["instId"], int(row["cTime"])
    side = "short" if row.get("posSide") == "short" or row.get("direction") == "short" else "long"
    r = {"decision_id": None, "sl": None, "tp": None, "reason": "", "source": "", "test": False}
    placed = {str(e.get("algoId")): e for e in events if e.get("event") == "trigger_placed"}
    p = (reg.get("positions") or {}).get(inst) or {}
    if p.get("decision_id") and p.get("side", side) == side and _near(p.get("opened"), c):
        r.update(decision_id=p["decision_id"], source="registry.positions")
        tp = placed.get(str(p.get("from_algo")))
        if tp:
            r.update(sl=tp.get("stop_loss"), tp=tp.get("take_profit"))
    for e in events:
        if r["decision_id"] or e.get("inst_id") != inst or not _near(e.get("time"), c):
            continue
        ev = e.get("event")
        if ev in ("trigger_filled", "trigger_fired") and str(e.get("algoId")) in placed:   # R12：新事件名 trigger_fired，旧记录 trigger_filled 兼容
            tp = placed[str(e["algoId"])]
            r.update(decision_id=tp.get("decision_id"), sl=tp.get("stop_loss"), tp=tp.get("take_profit"),
                     source=f"demo_events.trigger_filled({e['algoId']})")
        elif ev == "open_ok" and e.get("decision_id"):
            r.update(decision_id=e["decision_id"], sl=e.get("sl"), tp=e.get("tp"), source="demo_events.open_ok")
        elif ev == "eth_backfill_ok" or (ev or "").endswith("backfill_ok"):
            r.update(sl=e.get("sl"), tp=e.get("tp"), source=f"demo_events.{ev}")
            sl = _f(e.get("sl"))
            want = "sell" if side == "short" else "buy"
            cand = [t for t in papers if t.get("inst_id") == inst and t.get("side") == want and t.get("signal_id")
                    and sl is not None and abs((_f(t.get("sl_px"), 0) or 0) - sl) <= max(0.02, sl * 1e-4)]
            if cand:
                t = cand[-1]
                r.update(decision_id=t["signal_id"], reason=t.get("entry_reason", ""),
                         source=f"demo_events.{ev} → 纸面仓 {t['trade_id']}")
    if not r["decision_id"]:
        r["test"] = any(e.get("event") in TEST_EVENTS and e.get("inst_id") == inst and
                        (_near(e.get("time"), c, 60_000) or _near(e.get("time"), row.get("uTime") or c, 60_000))
                        for e in events)
    return r


def _exit_reason(row, sl, tp, ws_events) -> str:
    typ = str(row.get("type") or "")
    if typ in ("3", "4", "5"):
        return CLOSE_TYPE[typ]
    px, u = _f(row.get("closeAvgPx")), int(row.get("uTime") or 0)
    for e in ws_events:
        if e.get("inst_id") == row["instId"] and e.get("kind") in ("stop_loss", "take_profit") and _near(e.get("time"), u, 120_000):
            return "止损" if e["kind"] == "stop_loss" else "止盈"
    if px and sl and abs(px - float(sl)) / px <= 0.003:
        return "止损"
    if px and tp and abs(px - float(tp)) / px <= 0.003:
        return "止盈"
    return CLOSE_TYPE.get(typ, "平仓")


def to_trade(row: dict, info: dict, ct_val: float, ws_events=()) -> dict:
    side = "short" if row.get("posSide") == "short" or row.get("direction") == "short" else "long"
    contracts = _f(row.get("closeTotalPos")) or _f(row.get("openMaxPos"), 0)
    qty = contracts * ct_val
    entry, exit_ = _f(row.get("openAvgPx"), 0), _f(row.get("closeAvgPx"), 0)
    lev = _f(row.get("lever"), 1) or 1
    margin = entry * qty / lev
    pnl = _f(row.get("realizedPnl"), 0)
    fee = -(_f(row.get("fee"), 0) or 0)
    return {"trade_id": f"DEMO-{row['posId']}-{row['cTime']}", "status": "closed", "mode": "okx_demo", "venue": "okx_demo",
            "inst_id": row["instId"], "inst_type": "SWAP", "side": "sell" if side == "short" else "buy", "pos_side": side,
            "sz": f"{qty:.8g}", "contracts": f"{contracts:g}", "notional_usd": f"{entry * qty:.6f}", "leverage": lev,
            "entry_time": _ts(row["cTime"]), "entry_px": f"{entry:g}",
            "sl_px": "" if info.get("sl") in (None, "") else f"{float(info['sl']):g}",
            "tp_px": "" if info.get("tp") in (None, "") else f"{float(info['tp']):g}",
            "exit_time": _ts(row["uTime"]), "exit_px": f"{exit_:g}",
            "exit_reason": _exit_reason(row, _f(info.get("sl")), _f(info.get("tp")), ws_events),
            "fee_usd": f"{fee:.6f}", "funding_usd": row.get("fundingFee") or "0", "gross_pnl_usd": row.get("pnl") or "",
            "pnl_usd": f"{pnl:.6f}", "pnl_pct": f"{pnl / margin * 100:.4f}" if margin else "0",
            "signal_id": info.get("decision_id") or "", "decision_id": info.get("decision_id") or "",
            "entry_reason": info.get("reason", ""), "link_source": info.get("source", ""),
            "pos_id": row["posId"], "close_type": row.get("type")}


def _api_rows() -> list[dict]:
    from desk import okx_demo as X
    from desk import demo_exec
    return demo_exec.positions_history()   # R5：翻页取全


def _ct_val(inst, cache) -> float:
    if inst in cache:
        return cache[inst]
    from desk import okx_demo as X
    cache[inst] = float(X.instrument(inst)["ctVal"])
    return cache[inst]


def closed_trades(rows: list[dict] | None = None, ct_vals: dict | None = None, use_cache=True) -> list[dict]:
    """模拟盘已平仓交易（不含冒烟测试单）。rows=None 时只读调用 OKX 模拟盘；失败则退回缓存。"""
    from desk import demo_exec, okx_demo as X
    cache = settings.read_json(CACHE, {}) if use_cache else {}
    if rows is None:
        try:
            rows = _api_rows()
        except Exception:
            return sorted(cache.values(), key=lambda t: t["exit_time"])
    events = settings.read_jsonl(X.EVENTS)
    ws = settings.read_jsonl(settings.STATE / "ledger" / "demo_ws_events.jsonl")
    reg, papers, cts = demo_exec.reg(), paper_trades(), dict(ct_vals or {})
    for row in rows:
        if str(row.get("type")) in ("1", "4") or not row.get("uTime"):
            continue          # 部分平仓：等全部平掉后作为一笔复盘
        info = link(row, events, reg, papers)
        if info["test"]:
            continue
        t = to_trade(row, info, _ct_val(row["instId"], cts), ws)
        cache[t["trade_id"]] = t
    if use_cache:
        settings.write_json(CACHE, cache)
    return sorted(cache.values(), key=lambda t: t["exit_time"])


def review_candidates(cfg, reviewed: set, demo: bool) -> list[dict]:
    """待复盘：本地纸面账本 closed（旧 BTC/ETH 纸面仓仍可能平仓）+ 模拟盘平仓，按 trade_id 去重。"""
    from desk import ledger
    out, seen = [], set(reviewed)
    pool = [t for t in ledger.store.read_trades() if t["status"] == "closed"]
    if demo:
        pool += closed_trades()
    for t in pool:
        if t["trade_id"] not in seen:
            seen.add(t["trade_id"]); out.append(t)
    return out
