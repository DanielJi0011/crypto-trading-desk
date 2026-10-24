"""纸面账本 + 护栏（OKX USDT 永续合约，逐仓，多空双向；只做纸面模拟）。

合约模拟：qty=张数折算的币数量；名义=qty×价格；保证金=名义/杠杆（逐仓）；开/平各收 taker 费（paper.swap_fee_rate）；
资金费按开仓时 OKX 当前资金费率每 8 小时计（多头在费率为正时付、空头收）；强平价（线性逐仓近似）：
多 = 入场×(1−1/杠杆+维持保证金率)，空 = 入场×(1+1/杠杆−维持保证金率)；止损必须先于强平价被触发，否则拒单。
trades.csv 沿用 ai-trader 格式（side=buy 表示多、sell 表示空；pnl_usd 为扣费与资金费后的净盈亏），
合约附加字段（杠杆、保证金、强平价、资金费）存于 positions_meta.json。

- 账本沿用 ai-trader 的 trades.csv / orders.jsonl 格式（state/ledger/journal/），护栏直接复用
  /workspace/ai-trader/guardrails.py 的 check()，止损/仓位/敞口/日亏损/下单次数/紧急停止都由它强制。
- 默认只做纸面成交：按 OKX 最新价 ± 滑点成交，双边 0.1% 手续费。
- 实盘：需要 config.mode=live 且存在 state/LIVE_APPROVED 文件（由用户明确批准后才创建）；
  即便满足，本版本也不实现实盘下单（直接拒绝），以保证绝不误下真实订单。"""
from __future__ import annotations

import datetime as _dt
import math

import pandas as pd

from desk import okx_data, settings

okx_client, guardrails, store = settings.ai_trader_modules()

ACCOUNT = settings.STATE / "ledger" / "account.json"
META = settings.STATE / "ledger" / "positions_meta.json"

# 可替换的行情函数（测试中注入）
def PRICE(inst):
    return okx_data.last_price(inst)


def BARS(inst):
    return okx_data.candles(inst, "1H", 400)


class LiveTradingDisabled(RuntimeError):
    pass


def assert_paper(cfg):
    if cfg.get("mode") == "paper":
        return
    if cfg.get("mode") == "live" and (settings.STATE / "LIVE_APPROVED").exists():
        raise LiveTradingDisabled("实盘需用户批准后另行启用；当前版本不含实盘下单代码")
    raise LiveTradingDisabled(f"mode={cfg.get('mode')} 未获批准，拒绝执行")


def _demo(fn, *args):
    """调用 desk/okx_demo.py 的函数（仅 startup 自检；禁止实盘）。任何异常都吞掉。旧纸面仓的平仓不会触发任何模拟盘动作。"""
    try:
        from desk import okx_demo
        getattr(okx_demo, fn)(*args)
    except Exception as e:  # noqa: BLE001
        try:
            from desk import okx_demo
            okx_demo.log_event("hook_error", fn=fn, error=str(e))
        except Exception:
            pass


# ------------------------------------------------------------------ 账户
def account(cfg) -> dict:
    a = settings.read_json(ACCOUNT, None)
    if a is None:
        a = {"initial": float(cfg["paper"]["initial_equity_usdt"]), "cash": float(cfg["paper"]["initial_equity_usdt"]),
             "created": settings.now_str(), "last_mark": {}}
        settings.write_json(ACCOUNT, a)
    return a


def save_account(a):
    settings.write_json(ACCOUNT, a)


def meta() -> dict:
    return settings.read_json(META, {})


def save_meta(m):
    settings.write_json(META, m)


def open_trades() -> list[dict]:
    return [r for r in store.read_trades() if r.get("status") == "open"]


SIGN = {"long": 1, "short": -1}


def side_of(r) -> str:
    return "short" if r.get("side") == "sell" else "long"


def swap_cfg(cfg) -> dict:
    d = {"leverage_max": 5, "maint_margin_rate": 0.005, "funding_fallback_rate": 0.0001, "fee_rate": 0.0005}
    return {**d, **(cfg.get("swap") or {})}


def liq_price(side, entry, lev, mmr) -> float:
    return entry * (1 - 1 / lev + mmr) if side == "long" else entry * (1 + 1 / lev - mmr)


def FUNDING(inst) -> float | None:   # 可替换（测试注入）
    try:
        return okx_data.funding_rate(inst)
    except Exception:
        return None


def funding_cost(cfg, r, m, px=None, now=None) -> float:
    """到当前为止的资金费（正=支出）。按开仓时记录的费率，每 8 小时结算一次的近似（按持仓时长线性累计）。"""
    now = now or settings.now()
    t0 = m.get("entry_ts_ms") or int(now.timestamp() * 1000)
    hours = max(0.0, (now.timestamp() * 1000 - t0) / 3.6e6)
    rate = m.get("funding_rate", swap_cfg(cfg)["funding_fallback_rate"])
    return SIGN[side_of(r)] * float(r["sz"]) * float(px or r["entry_px"]) * rate * hours / 8


def book(cfg, prices: dict | None = None) -> dict:
    a = account(cfg)
    mm = meta()
    tick = {c["inst_id"]: c["ticker"] for c in cfg["coins"]}
    pos = []
    for r in open_trades():
        m = mm.get(r["trade_id"], {})
        px = (prices or {}).get(r["inst_id"]) or float(r["entry_px"])
        qty, ent, sd = float(r["sz"]), float(r["entry_px"]), side_of(r)
        lev = float(m.get("leverage", 1)); margin = float(m.get("margin_usd", qty * ent / lev))
        fund = funding_cost(cfg, r, m)
        upnl = SIGN[sd] * qty * (px - ent) - float(r["fee_usd"] or 0) - fund
        pos.append({"trade_id": r["trade_id"], "inst_id": r["inst_id"], "ticker": tick.get(r["inst_id"], r["inst_id"]),
                    "side": sd, "leverage": lev, "margin": margin, "notional": qty * px, "liq_px": m.get("liq_px"),
                    "funding": fund, "qty": qty, "avg_px": ent, "price": px, "value": margin + upnl,
                    "sl_px": float(r["sl_px"] or 0) or None, "tp_px": float(r["tp_px"] or 0) or None,
                    "upnl": upnl, "entry_time": r["entry_time"], "deadline": m.get("deadline")})
    eq = a["cash"] + sum(p["value"] for p in pos)
    return {"cash": a["cash"], "equity": eq, "initial": a["initial"], "positions": pos,
            "exposure": sum(p["notional"] for p in pos), "margin": sum(p["margin"] for p in pos)}


# ------------------------------------------------------------------ 护栏
class Guard:
    def __init__(self, ok, reasons, checks):
        self.ok, self.reasons, self.checks = ok, reasons, checks


def check_open(cfg, inst, qty, px, sl, tp, equity, side="long", lev=1.0):
    """合约开仓护栏：紧急停止、品种池、杠杆≤上限、单笔保证金≤权益 max_position_pct%、总名义≤max_total_notional_pct%、
    持仓数≤max_open_positions、每币一仓（不加仓）、强制止损（多在下/空在上、距离≤max_stop_loss_pct%）、止损先于强平、
    当日亏损上限、每日下单上限。"""
    g, sw = cfg["guardrails"], swap_cfg(cfg)
    reasons, checks = [], {}
    fail = reasons.append
    if settings.kill_active():
        fail("[KILL] 紧急停止文件存在，禁止开仓")
    if settings.halt_active():
        fail("[HALT] 全冻结文件存在，禁止开仓")
    if inst not in [c["inst_id"] for c in cfg["coins"]]:
        fail(f"[UNIVERSE] {inst} 不在品种池")
    if g.get("allow_withdrawal"):
        fail("[WITHDRAW] allow_withdrawal 必须为 false")
    from desk import config_safety
    if not (1 <= lev <= config_safety.max_open_leverage(cfg)):
        fail(f"[LEVERAGE] 杠杆 {lev} 超出新开仓上限 1~{config_safety.max_open_leverage(cfg):g}")
    notional = qty * px; margin = notional / max(lev, 1e-9)
    checks.update(notional_usd=round(notional, 4), margin_usd=round(margin, 4), leverage=lev)
    if qty <= 0 or px <= 0:
        fail("[SIZE] 数量与价格必须为正")
    if margin > equity * g["max_position_pct"] / 100 + 1e-9:
        fail(f"[POSITION] 保证金 {margin:.2f} 超过单笔上限 {equity * g['max_position_pct'] / 100:.2f}（{g['max_position_pct']}%）")
    if notional < g.get("min_order_usd", 0):
        fail(f"[MIN_ORDER] 名义价值低于 {g.get('min_order_usd')} USD")
    opens = open_trades()
    if len(opens) >= g.get("max_open_positions", 3):
        fail(f"[MAX_POS] 未平仓数 {len(opens)} 已达上限")
    if any(r["inst_id"] == inst for r in opens):
        fail("[ONE_PER_COIN] 该币已有持仓，不加仓")
    exp = sum(float(r["notional_usd"] or 0) for r in opens)
    max_tot = g.get("max_total_notional_pct") or g.get("max_total_exposure_pct", 60) * float(sw["leverage_max"])
    if exp + notional > equity * max_tot / 100 + 1e-9:
        fail(f"[EXPOSURE] 总名义将达 {exp + notional:.2f}，超过 {equity * max_tot / 100:.2f}（{max_tot}%）")
    liq = liq_price(side, px, max(lev, 1e-9), float(sw["maint_margin_rate"]))
    checks["liq_px"] = liq
    if sl is None:
        fail("[STOP_LOSS] 开仓必须附带止损")
    else:
        if side == "long" and sl >= px:
            fail(f"[STOP_LOSS] 多单止损 {sl} 必须低于入场 {px:.6g}")
        if side == "short" and sl <= px:
            fail(f"[STOP_LOSS] 空单止损 {sl} 必须高于入场 {px:.6g}")
        dist = abs(px - sl) / px * 100
        checks["sl_distance_pct"] = round(dist, 3)
        if dist > g.get("max_stop_loss_pct", 100):
            fail(f"[STOP_LOSS] 止损距离 {dist:.2f}% 超过上限 {g.get('max_stop_loss_pct')}%")
        if (side == "long" and sl <= liq) or (side == "short" and sl >= liq):
            fail(f"[LIQUIDATION] 止损 {sl:.6g} 不会先于强平价 {liq:.6g} 触发")
    pnl = guardrails._today_realized_pnl()
    if pnl <= -equity * g["max_daily_loss_pct"] / 100:
        fail(f"[DAILY_LOSS] 当日已实现亏损 {pnl:.2f} 达到上限，今日禁止开仓")
    n, lim = guardrails._today_orders("paper"), int(g.get("max_orders_per_day") or 0)
    if lim > 0 and n >= lim:   # 0 = 不限
        fail(f"[MAX_ORDERS] 今日开仓单 {n} 已达上限")
    return Guard(not reasons, reasons, checks)


def kill_switch_path(cfg=None):
    """R7：唯一路径常量 settings.KILL_PATH（state/KILL）。判定请用 settings.kill_active()（含旧路径过渡兼容）。"""
    return settings.KILL_PATH


# ------------------------------------------------------------------ 成交
def _fill(cfg, inst, side, px=None):
    """side: buy/sell（成交方向），按滑点对己方不利。"""
    px = px or PRICE(inst)
    s = cfg["paper"]["slippage_bp"] / 1e4
    return px * (1 + s) if side == "buy" else px * (1 - s)


def _log_order(rec):
    rec.setdefault("date", store.today_str())
    rec.setdefault("time", settings.now_str())
    rec.setdefault("mode", "paper")
    store.append_jsonl(store.ORDERS_LOG, rec)


def open_position(cfg, inst, notional, sl, tp, horizon_days, decision_id, reason, equity, ref_px=None,
                  side="long", leverage=1.0):
    """纸面开仓（永续，逐仓）。notional=名义金额；保证金=名义/杠杆。先过护栏，不通过则不成交。返回 (ok, info)。"""
    assert_paper(cfg)
    a = account(cfg)
    sw = swap_cfg(cfg)
    lev = float(leverage)
    px = _fill(cfg, inst, "buy" if side == "long" else "sell", ref_px)
    fee_rate = float(sw["fee_rate"])
    notional = min(notional, a["cash"] * lev / (1 + fee_rate * lev))
    qty = math.floor(notional / px * 1e8) / 1e8
    g = check_open(cfg, inst, qty, px, sl, tp, equity, side, lev)
    if not g.ok:
        _log_order({"inst_id": inst, "side": "buy" if side == "long" else "sell", "pos_side": side, "sz": qty, "px": px,
                    "result": "rejected", "close": False, "reasons": g.reasons, "decision_id": decision_id})
        return False, {"reasons": g.reasons, "checks": g.checks}
    fee = qty * px * fee_rate
    margin = qty * px / lev
    a["cash"] -= margin + fee
    tid = store.new_id("P")
    rows = store.read_trades()
    rows.append({"trade_id": tid, "status": "open", "mode": "paper", "thesis_version": "ta-desk-swap", "inst_id": inst,
                 "inst_type": "SWAP", "side": "buy" if side == "long" else "sell", "sz": f"{qty:.8f}",
                 "notional_usd": f"{qty * px:.6f}", "entry_time": settings.now_str(), "entry_px": f"{px:.8g}",
                 "sl_px": f"{sl:.8g}", "tp_px": f"{tp:.8g}" if tp else "", "fee_usd": f"{fee:.6f}",
                 "signal_id": decision_id, "entry_reason": reason[:300], "note": f"{side} {lev:g}x 逐仓"})
    store.write_trades(rows)
    fr = FUNDING(inst)
    m = meta()
    m[tid] = {"decision_id": decision_id, "horizon_days": horizon_days,
              "deadline": (settings.now() + _dt.timedelta(days=horizon_days)).strftime("%Y-%m-%d %H:%M:%S%z"),
              "initial_sl": sl, "entry_ts_ms": int(settings.now().timestamp() * 1000), "side": side, "leverage": lev,
              "margin_usd": margin, "liq_px": g.checks["liq_px"],
              "funding_rate": fr if fr is not None else float(sw["funding_fallback_rate"]),
              "funding_source": "OKX 当前资金费率" if fr is not None else "配置常数"}
    save_meta(m)
    a["last_mark"][tid] = m[tid]["entry_ts_ms"]
    save_account(a)
    _log_order({"inst_id": inst, "side": "buy" if side == "long" else "sell", "pos_side": side, "sz": qty, "px": px,
                "fee": fee, "result": "placed", "close": False, "trade_id": tid, "decision_id": decision_id, "sl": sl,
                "tp": tp, "leverage": lev, "margin": margin, "liq_px": g.checks["liq_px"]})
    return True, {"trade_id": tid, "qty": qty, "px": px, "fee": fee, "side": side, "leverage": lev, "margin": margin,
                  "notional": qty * px, "liq_px": g.checks["liq_px"], "checks": g.checks}


def close_position(cfg, trade_id, reason, px=None, fraction=1.0, when=None):
    """平仓或部分减仓（fraction<1 时拆分出一条已平仓记录）。返回已平仓记录。"""
    assert_paper(cfg)
    rows = store.read_trades()
    i = next(k for k, r in enumerate(rows) if r["trade_id"] == trade_id and r["status"] == "open")
    r = rows[i]
    inst, sd = r["inst_id"], side_of(r)
    px = px if px is not None else _fill(cfg, inst, "sell" if sd == "long" else "buy")
    fee_rate = float(swap_cfg(cfg)["fee_rate"])
    m = meta()
    mt = m.get(trade_id, {})
    lev = float(mt.get("leverage", 1))
    qty_all = float(r["sz"])
    if fraction < 0.999:
        qty = math.floor(qty_all * fraction * 1e8) / 1e8
        part = dict(r)
        k = sum(1 for x in rows if x["trade_id"].startswith(trade_id + "-r")) + 1
        part["trade_id"] = f"{trade_id}-r{k}"
        part["sz"] = f"{qty:.8f}"
        part["notional_usd"] = f"{float(r['notional_usd']) * qty / qty_all:.6f}"
        part["fee_usd"] = f"{float(r['fee_usd']) * qty / qty_all:.6f}"
        r["sz"] = f"{qty_all - qty:.8f}"
        r["notional_usd"] = f"{float(r['notional_usd']) * (qty_all - qty) / qty_all:.6f}"
        r["fee_usd"] = f"{float(r['fee_usd']) * (qty_all - qty) / qty_all:.6f}"
        rows.insert(i + 1, part)
        target = part
        f = qty / qty_all
        m[part["trade_id"]] = dict(mt, parent=trade_id, margin_usd=float(mt.get("margin_usd", 0)) * f)
        if trade_id in m:
            m[trade_id]["margin_usd"] = float(mt.get("margin_usd", 0)) * (1 - f)
    else:
        qty, target = qty_all, r
    tm = m.setdefault(target["trade_id"], dict(mt))
    entry = float(target["entry_px"])
    margin = float(tm.get("margin_usd", float(target["notional_usd"]) / lev))
    fee = qty * px * fee_rate
    fund = funding_cost(cfg, target, tm)
    gross = SIGN[sd] * qty * (px - entry)
    if reason == "强平":
        gross = -margin   # 逐仓：最多亏光该仓保证金
        fee, fund = 0.0, 0.0
    pnl = gross - fee - fund - float(target["fee_usd"])
    target.update({"status": "closed", "exit_time": when or settings.now_str(), "exit_px": f"{px:.8g}",
                   "exit_reason": reason, "fee_usd": f"{float(target['fee_usd']) + fee:.6f}",
                   "pnl_usd": f"{pnl:.6f}", "pnl_pct": f"{pnl / margin * 100:.4f}"})
    tm["funding_usd"] = fund
    save_meta(m)
    store.write_trades(rows)
    a = account(cfg)
    a["cash"] += max(0.0, margin + gross - fee - fund)
    a["last_mark"].pop(target["trade_id"], None)
    save_account(a)
    _log_order({"inst_id": inst, "side": "sell" if sd == "long" else "buy", "pos_side": sd, "sz": qty, "px": px, "fee": fee,
                "funding": fund, "result": "placed", "close": True, "trade_id": target["trade_id"], "reason": reason})
    return target


def move_stop(cfg, trade_id, new_sl):
    """只允许收紧止损：多单只能上移，空单只能下移（allow_stop_widen=false 时）。"""
    rows = store.read_trades()
    for r in rows:
        if r["trade_id"] == trade_id and r["status"] == "open":
            old = float(r["sl_px"])
            widen = new_sl < old if side_of(r) == "long" else new_sl > old
            if widen and not cfg["manager"].get("allow_stop_widen"):
                return False, f"禁止放宽止损（{old:.6g}→{new_sl:.6g}）"
            r["sl_px"] = f"{new_sl:.8g}"
            store.write_trades(rows)
            return True, f"止损 {old:.6g}→{new_sl:.6g}"
    return False, "未找到持仓"


def mark_to_market(cfg, now: _dt.datetime | None = None) -> list[dict]:
    """用上次检查以来的已收盘K线检查止损/止盈/强平（多：Low 触止损、High 触止盈；空：High 触止损、Low 触止盈；
    同一根K线两者都触及按止损；跳空按开盘价；成交价越过强平价按强平），再检查持仓到期。返回本次平仓的记录。"""
    now = now or settings.now()
    closed = []
    a = account(cfg)
    m = meta()
    for r in open_trades():
        tid, inst, sd = r["trade_id"], r["inst_id"], side_of(r)
        sl = float(r["sl_px"]); tp = float(r["tp_px"]) if r.get("tp_px") else None
        liq = m.get(tid, {}).get("liq_px")
        since = a["last_mark"].get(tid) or m.get(tid, {}).get("entry_ts_ms") or 0
        bars = BARS(inst)
        bars = bars[bars.ts >= since] if len(bars) else bars
        hit = None
        for b in bars.itertuples():
            bt = pd.Timestamp(b.ts, unit="ms", tz="UTC").tz_convert(settings.TZ).strftime("%Y-%m-%d %H:%M:%S%z")
            if sd == "long":
                if b.Low <= sl:
                    px = min(b.Open, sl)
                    hit = ("强平", liq, bt) if liq and px <= liq else ("止损", px, bt); break
                if liq and b.Low <= liq:
                    hit = ("强平", liq, bt); break
                if tp and b.High >= tp:
                    hit = ("止盈", max(b.Open, tp), bt); break
            else:
                if b.High >= sl:
                    px = max(b.Open, sl)
                    hit = ("强平", liq, bt) if liq and px >= liq else ("止损", px, bt); break
                if liq and b.High >= liq:
                    hit = ("强平", liq, bt); break
                if tp and b.Low <= tp:
                    hit = ("止盈", min(b.Open, tp), bt); break
        if len(bars):
            step = int(bars.ts.diff().median()) if len(bars) > 1 else 3600_000   # 支持 1H / 5m 等任意周期
            a = account(cfg); a["last_mark"][tid] = int(bars.ts.iloc[-1]) + step; save_account(a)
        if hit:
            closed.append(close_position(cfg, tid, hit[0], px=hit[1], when=hit[2]))
            continue
        dl = m.get(tid, {}).get("deadline")
        if dl and now >= _dt.datetime.strptime(dl, "%Y-%m-%d %H:%M:%S%z"):
            closed.append(close_position(cfg, tid, "持有期到期"))
    return closed


def migrate_to_swap(cfg) -> str:
    """把旧现货账本迁移为合约账本：旧持仓视为 1x 多单（保证金=名义），已平仓记录不动。幂等。"""
    rows = store.read_trades()
    m = meta(); n = 0
    for r in rows:
        if r["status"] == "open" and r["trade_id"] not in m or "side" not in m.get(r["trade_id"], {"side": 1}):
            mt = m.setdefault(r["trade_id"], {})
            if "side" not in mt and r["status"] == "open":
                mt.update(side="long", leverage=1.0, margin_usd=float(r["notional_usd"]),
                          liq_px=0.0, funding_rate=0.0, funding_source="迁移（现货无资金费）"); n += 1
    save_meta(m)
    return f"迁移 {n} 条旧现货持仓为 1x 多单"
