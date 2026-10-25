"""交易经理：LLM（深度模型）给出结构化决策，代码层做角色加权共识、仓位规则与硬护栏，然后执行（execution.venue=okx_demo 时直连 OKX 模拟盘，禁止实盘；paper 时纸面执行）。"""
from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, Field

from desk import ledger, okx_data, roles, settings
from desk import llm as dllm
from desk.roles import INIT_WEIGHT, ROLE_CN, ROLE_KEYS
from desk.stances import INVALID_STATUS, normalize as _norm_stance, role_texts, stance_status

WEIGHTS = settings.STATE / "weights.json"
DECISIONS = settings.STATE / "decisions.jsonl"
# "close" = 交易经理对已有持仓平仓/减仓：是仓位管理动作，不是对未来价格方向的预测 → 共识里记 0，不参与方向打分
DIR_NUM = {"bullish": 1, "bearish": -1, "neutral": 0, "close": 0}
ACT_CN = {"hold(交易经理否决)": "不入场（交易经理否决）", "hold(交易经理缺席)": "不开新仓（交易经理缺席）", "buy": "开多", "short": "开空", "hold": "持有/观望", "sell": "平仓", "reduce": "减仓", "pending": "挂条件入场单", "hold(止损冷却)": "不开仓（止损冷却）", "hold(止盈后不追单)": "不开仓（止盈后交易经理否决追单）"}


# 投票角色：组合经理只负责执行（过护栏、在 OKX 模拟盘下市价单/挂条件单），不投票、不计权重；交易经理作为一个投票角色
VOTERS = [r for r in ROLE_KEYS if r != "portfolio_manager"] + ["trade_manager"]
TM_INIT_WEIGHT = 1.0


def weights(cfg=None) -> dict:
    """决策用权重。learning.apply_weights=false（学习冻结）时一律用初始权重，学到的权重只在影子文件里。"""
    cfg = cfg if cfg is not None else settings.load_config()
    w = settings.read_json(WEIGHTS, {}) if (cfg.get("learning") or {}).get("apply_weights", False) else {}
    # 只给投票角色权重：组合经理只执行、不投票，不在权重里
    out = {r: float(w.get(r, INIT_WEIGHT[r])) for r in ROLE_KEYS if r != "portfolio_manager"}
    out["trade_manager"] = float(w.get("trade_manager", TM_INIT_WEIGHT))
    return out


def valid_voters(st: dict) -> list:
    """L3：有效投票角色（排除 failed/absent/data_missing）。"""
    return [r for r in VOTERS if r in st and stance_status(st[r]) not in INVALID_STATUS]


def consensus(st: dict, w: dict) -> float:
    rs = valid_voters(st)
    num = sum(w[r] * DIR_NUM[st[r]["direction"]] * float(st[r].get("confidence") or 0) for r in rs)
    den = sum(w[r] for r in rs)
    return num / den if den else 0.0


class ManagerDecision(BaseModel):
    action: Literal["buy", "short", "pending", "hold", "sell", "reduce"] = Field(description="buy=现价开多；short=现价开空；pending=挂条件入场单（必须完整填写 entry_* 字段）；hold=不动/观望（不投多空票，条件单字段一律忽略）；sell=平掉现有持仓（多或空）；reduce=部分减仓")
    size_pct: float = Field(default=0, description="已废弃（L5）：仓位由按风险定仓决定，此字段不再影响下单；请改填 risk_scale")
    risk_scale: float = Field(default=1.0, description="风险缩放 0~1（L5）：在按风险定仓的基础上只降不升，1=按规则全额，0.5=减半，0=否决本币开仓")
    leverage: float = Field(default=1, description="杠杆倍数（新开仓上限见 desk/config_safety.max_open_leverage，当前 1 倍，只作上限；逐仓）；止损必须先于强平价触发")
    reduce_fraction: float | None = Field(default=None, description="仅 action=reduce 时填写：卖出现有持仓的比例（0~1）；其他动作不要填")
    entry: float | None = Field(default=None, description="计划入场价（USDT），市价执行时可写当前价")
    stop_loss: float | None = Field(default=None, description="止损价（USDT，开仓必须给出：做多低于现价，做空高于现价）")
    take_profit: float | None = Field(default=None, description="止盈价（USDT），可省略")
    new_stop: float | None = Field(default=None, description="已有持仓时希望收紧到的新止损价（多单只能上移、空单只能下移），可省略")
    new_tp: float | None = Field(default=None, description="已有持仓（持有或减仓）时希望调整到的新止盈价，可调近或调远，但必须在现价盈利一侧（多单高于现价、空单低于现价）且不会立即触发；可省略")
    invalidation: str | None = Field(default=None, description="一句话：什么情况说明判断错了（必填）")
    horizon_days: int = Field(default=5, description="持有周期（天，1~30）")
    confidence: float = Field(default=0.5, description="0~1")
    rationale: str = Field(description="中文决策理由（≤200字），要说明采纳/否决了哪些角色的观点")
    # —— 条件入场（挂单）：不想现价买、但在某个价位愿意买时填写
    entry_side: Literal["long", "short"] | None = Field(default=None, description="条件单方向：long=做多，short=做空")
    entry_trigger_px: float | None = Field(default=None, description="条件入场触发价（USDT）；不挂条件单就省略")
    entry_condition: Literal["gte", "lte"] | None = Field(default=None, description="gte=价格涨到≥触发价时开仓；lte=跌到≤触发价时开仓（多空都适用）")
    entry_stop_loss: float | None = Field(default=None, description="条件单成交后的止损价（做多低于触发价，做空高于触发价）")
    entry_take_profit: float | None = Field(default=None, description="条件单成交后的止盈价，可省略")
    entry_size_pct: float | None = Field(default=None, description="条件单保证金占权益百分比（0~20）")
    entry_expire_hours: float | None = Field(default=None, description="条件单有效期（小时），过期自动撤销")
    entry_reason: str = Field(default="", description="为什么选这个入场价位（中文，≤80字）")


def tm_stance(dec: "ManagerDecision", mk: dict, pos=None) -> dict:
    """把交易经理的结构化决策转换为一个投票角色的立场。"""
    if dec.action in ("buy", "short"):
        d, ep, cond = ("bullish" if dec.action == "buy" else "bearish"), dec.entry or mk["last"], "now"
    elif dec.action in ("sell", "reduce") and pos:
        # 平空/减空不等于看多、平多/减多不等于看空：记为「平仓」方向，不计入多空方向打分
        d, ep, cond = "close", None, None
    elif dec.action == "pending" and pending_complete(dec):
        # B1：只有动作标签=挂单 且条件入场字段完整，条件单字段才生效；hold/观望一律中性
        d, ep, cond = ("bullish" if dec.entry_side == "long" else "bearish"), dec.entry_trigger_px, dec.entry_condition
    else:
        d, ep, cond = "neutral", None, None
    neu = d in ("neutral", "close")
    return {"direction": d, "confidence": max(0.0, min(1.0, float(dec.confidence or 0))),
            "target": None if neu else (dec.entry_take_profit if d != "neutral" and dec.action == "pending" else dec.take_profit),
            "stop": None if neu else (dec.entry_stop_loss if dec.action == "pending" else dec.stop_loss),
            "horizon_days": dec.horizon_days, "entry_px": ep, "entry_condition": cond,
            "reason": (dec.entry_reason or dec.rationale or "")[:80], "source": "交易经理结构化"}


def pending_complete(dec) -> bool:
    return bool(dec.entry_side and dec.entry_trigger_px and dec.entry_condition and dec.entry_stop_loss
                and dec.entry_size_pct is not None and float(dec.entry_size_pct) > 0)


ENTRY_FIELDS = ("entry_side", "entry_trigger_px", "entry_condition", "entry_stop_loss", "entry_take_profit", "entry_size_pct",
                "entry_expire_hours")


def tm_field_issues(m: dict, pos=None) -> dict:
    """B1/B4：动作标签与字段的冲突（以动作标签为准）和必填字段缺失。m = ManagerDecision.model_dump()。"""
    a, conflicts, missing = m.get("action"), [], []
    filled = [k for k in ENTRY_FIELDS if m.get(k) not in (None, "")]
    if a != "pending" and filled:
        conflicts.append(f"动作标签={a}，条件单字段 {','.join(filled)} 已忽略（以动作标签为准）")
    if a == "pending":
        need = [k for k in ("entry_side", "entry_trigger_px", "entry_condition", "entry_stop_loss", "entry_size_pct") if m.get(k) in (None, "")]
        if need:
            conflicts.append(f"动作标签=挂单但缺 {','.join(need)}，按观望处理")
        elif float(m.get("entry_size_pct") or 0) <= 0:
            conflicts.append("动作标签=挂单但 entry_size_pct<=0，按观望处理")
    if a == "hold" and float(m.get("size_pct") or 0) > 0:
        conflicts.append(f"动作标签=观望但 size_pct={m.get('size_pct')}，已忽略")
    if a != "reduce" and m.get("reduce_fraction") is not None:
        conflicts.append(f"动作标签={a}，reduce_fraction={m.get('reduce_fraction')} 已忽略")
    if a == "reduce" and m.get("reduce_fraction") is None:
        missing.append("reduce_fraction")
    if not (m.get("invalidation") or "").strip():
        missing.append("invalidation")
    return {"conflicts": conflicts, "missing": missing}


def tm_status(dec: "ManagerDecision | None", pos=None, mk=None) -> str:
    """交易经理结构化决定 → "absent"（调用失败）/ "veto"（不入场）/ "go"（有方向，交给加权聚合）。
    以方向投票为准，不看动作标签：action=hold、size_pct=0 但给了 entry_side+entry_trigger_px 的条件入场计划 = 有方向，不算否决。
    只有以下才算否决：方向为中性（hold 且无条件入场计划）；或明确不入场——无持仓时 sell/reduce、
    置信度 0、条件入场计划显式 entry_size_pct=0。"""
    if dec is None:
        return "absent"
    if float(dec.confidence or 0) <= 0:
        return "veto"
    if dec.action not in ("sell", "reduce") and float(dec.risk_scale if dec.risk_scale is not None else 1) <= 0:
        return "veto"   # L5：risk_scale<=0 等同否决开仓
    if dec.action in ("sell", "reduce") and not pos:
        return "veto"
    d = tm_stance(dec, mk or {"last": dec.entry or 0}, pos)["direction"]
    return "veto" if d == "neutral" else "go"   # "close"（有持仓的 sell/reduce）= go，照常执行平仓/减仓


def tm_label_mismatch(m: dict) -> str | None:
    """动作标签与字段冲突时返回说明（日报标出）；规则：以动作标签为准（B1）。"""
    c = tm_field_issues(m)["conflicts"]
    return "；".join(c) if c else None


def _reduce_fraction(tm, notes) -> float:
    """B4：reduce 缺 reduce_fraction 时记字段缺失，按一半执行并写明（不静默）。"""
    if tm.reduce_fraction is None:
        notes.append("字段缺失：reduce_fraction（减仓比例），按 50% 执行")
        return 0.5
    return min(max(float(tm.reduce_fraction), 0.1), 0.9)


TM_VETO_NOTE = "交易经理否决：不下现价单、不挂条件单"
TM_ABSENT_NOTE = "交易经理缺席，不开新仓"


def aggregate(cfg, st: dict, w: dict, mk: dict) -> dict:
    """按角色权重×置信度聚合：方向（共识 S）、仓位、入场价位与条件、止损、止盈、周期。"""
    M, P = cfg["manager"], cfg.get("pending", {})
    st = {r: _norm_stance(s) for r, s in st.items()}   # B1：中性/平仓立场的价位字段不参与聚合
    S = consensus(st, w)
    last = mk["last"]
    th = float(M.get("consensus_threshold", 0.1))
    side = "long" if S > th else ("short" if S < -th else None)
    want = "bullish" if side != "short" else "bearish"
    sg = 1 if side != "short" else -1
    nv, need = len(valid_voters(st)), int(M.get("min_valid_voters", 0) or 0)
    if need and nv < need:   # L3：有效票不足 → 不交易
        side = None
    bulls = [r for r in valid_voters(st) if st[r]["direction"] == want and (st[r].get("confidence") or 0) > 0]

    def wavg(vals):
        vals = [(w[r] * float(st[r]["confidence"]), v) for r, v in vals if v]
        den = sum(a for a, _ in vals)
        return sum(a * v for a, v in vals) / den if den else None
    band = float(P.get("immediate_band_pct", 0.3)) / 100
    from desk import sizing
    rc = sizing.rcfg(cfg)
    notes = []
    plan = sizing.choose_plan(st, w, bulls, side or "long", last, band, float(rc["plan_conflict_pct"])) if bulls else {"group": None, "conflict": False, "scores": {}}
    conflict = bool(side and plan["conflict"])
    entry, cond = (plan.get("entry") or last), plan.get("condition") or "now"
    atr = mk.get("atr14")
    if atr:   # R2：止损按 ATR，止盈按 R 倍数；角色给的止损/目标只作参考
        stop, tp, _d = sizing.stop_take(side or "long", entry, float(atr), rc)
    else:     # 无 ATR（仅旧测试/纸面路径）：沿用角色加权
        stop = wavg([(r, float(st[r]["stop"])) for r in bulls if st[r].get("stop") and sg * (entry - float(st[r]["stop"])) > 0])
        tp = wavg([(r, float(st[r]["target"])) for r in bulls if st[r].get("target") and sg * (float(st[r]["target"]) - entry) > 0])
    if conflict:
        notes.append(f"入场方案冲突（各组得分 {plan['scores']}，差距 < {float(rc['plan_conflict_pct']):.0%}），不开仓（plan_conflict）")
    hz = plan.get("horizon") or wavg([(r, float(st[r].get("horizon_days") or 0)) for r in bulls])
    cap = min(M["max_size_pct"], cfg["guardrails"]["max_position_pct"])
    size = cap * max(0.0, min(1.0, abs(S) / float(M.get("full_size_consensus", 0.6))))
    if side and abs(S) < M["half_size_below"]:
        size /= 2; notes.append(f"共识偏弱（|S|={abs(S):.3f}<{M['half_size_below']}），仓位减半")
    buy = side is not None and bool(bulls) and not conflict and plan.get("group") is not None
    if need and nv < need:
        notes.append(f"有效票不足（{nv}<{need}），不交易")
    elif not buy:
        notes.append(f"加权共识 S={S:+.3f} 在 ±{th} 之间或无同向角色，不交易")
    return {"S": round(S, 4), "buy": buy, "side": side, "entry_px": entry, "condition": cond, "stop_loss": stop, "take_profit": tp,
            "size_pct": round(size, 3), "horizon_days": int(min(max(round(hz or 5), 1), 30)), "notes": notes,
            "contributors": {r: st[r].get("entry_px") for r in bulls},
            "plan_group": plan.get("group"), "plan_scores": plan.get("scores"), "plan_conflict": conflict, "valid_voters": nv}


def market_ctx(inst: str) -> dict:
    d = okx_data.candles(inst, "1Dutc", 60)
    c, h, l = d.Close, d.High, d.Low
    tr = (h - l).combine((h - c.shift()).abs(), max).combine((l - c.shift()).abs(), max)
    return {"last": okx_data.last_price(inst), "close": float(c.iloc[-1]), "bar_date": str(d.Date.iloc[-1].date()),
            "ret_5d": float(c.iloc[-1] / c.iloc[-6] - 1) * 100, "ret_20d": float(c.iloc[-1] / c.iloc[-21] - 1) * 100,
            "atr14": float(tr.tail(14).mean()), "hi_20": float(h.tail(20).max()), "lo_20": float(l.tail(20).min())}


def _prompt(cfg, coin, mk, pos, bk, st, w, S, texts):
    g = cfg["guardrails"]
    lines = [f"你是加密货币交易台的交易经理，负责对 {coin['name']}（OKX 永续 {coin['inst_id']}）做今日最终执行决策。",
             f"标的是 OKX USDT 永续合约（OKX 模拟盘 Demo Trading 执行，逐仓），可以做多也可以做空，新开仓杠杆上限 {__import__('desk.config_safety', fromlist=['x']).max_open_leverage(cfg):g} 倍（代码强制）。",
             f"硬性规则（代码会强制，违反会被拒单）：单笔保证金≤权益 {g['max_position_pct']}%，总名义≤权益 {g.get('max_total_notional_pct') or g['max_total_exposure_pct'] * ledger.swap_cfg(cfg)['leverage_max']:g}%，"
             f"开仓必须带止损（做多在下方、做空在上方）且止损距离≤{g['max_stop_loss_pct']}%，止损必须先于强平价触发，止损只能收紧，每币一仓不加仓。",
             "", "## 行情（OKX，已收盘日线）",
             f"最新价 {mk['last']:.6g}；最近日线 {mk['bar_date']} 收 {mk['close']:.6g}；5日 {mk['ret_5d']:+.2f}%；20日 {mk['ret_20d']:+.2f}%；"
             f"ATR14 {mk['atr14']:.6g}（{mk['atr14'] / mk['last'] * 100:.2f}%）；20日高 {mk['hi_20']:.6g} 低 {mk['lo_20']:.6g}",
             "", "## 账户（OKX 模拟盘）" if "live_equity" in bk else "## 账户（纸面）",
             f"权益基准 {bk['equity']:.2f} USDT（仓位%与护栏均按此计算"
             + (f"，基准={bk.get('equity_basis')}；模拟盘实时权益 {bk['live_equity']:.2f}" if "live_equity" in bk else "")
             + f"），现金 {bk['cash']:.2f}，当前敞口 {bk['exposure']:.2f}（{bk['exposure'] / bk['equity'] * 100:.1f}%）",
             f"按此基准：单笔保证金上限 {bk['equity'] * g['max_position_pct'] / 100:,.0f} USDT，总名义上限 "
             f"{bk['equity'] * (g.get('max_total_notional_pct') or g['max_total_exposure_pct'] * ledger.swap_cfg(cfg)['leverage_max']) / 100:,.0f} USDT，"
             f"当日亏损达 {bk['equity'] * g['max_daily_loss_pct'] / 100:,.0f} USDT 停止开仓；最多 {g.get('max_open_positions', 3)} 个仓、每币一仓。"]
    if pos:
        lines.append(f"本币持仓：{'空' if pos['side'] == 'short' else '多'}单（posSide={pos.get('pos_side') or pos['side']}）{pos['leverage']:g}x，{(-1 if pos['side'] == 'short' else 1) * abs(pos['qty']):+.8g} 枚（负数=空），强平价 {pos['liq_px']}，均价 {pos['avg_px']:.6g}，止损 {pos['sl_px']}，止盈 {pos['tp_px']}，"
                     f"浮盈亏 {pos['upnl']:+.2f} USDT，到期 {pos['deadline']}")
    else:
        lines.append("本币无持仓。")
    from desk import analysis as _an
    lines += ["", _an.pending_brief(bk)]
    lines += ["", f"## 各角色观点（权重为历史表现给出；加权共识 S={S:+.3f}，范围 -1~+1）",
              f"口径说明：这里的 S={S:+.3f} 是【不含你本人投票】的其余角色加权共识。你的结构化决定会作为一票（权重 {w.get('trade_manager', 1):g}）计入，"
              "代码最终共识（决策记录 consensus）因此会与此值不同，这是已知口径差异。理由中引用共识时请写明\"不含交易经理的 S\"。"]
    for r in ROLE_KEYS:
        s = st[r]
        wl = "只执行不投票" if r not in w else f"权重 {w[r]:.2f}"
        lines.append(f"- {ROLE_CN[r]}（{wl}）：{s['direction']} 置信 {s.get('confidence')} 目标 {s.get('target')} "
                     f"止损 {s.get('stop')} 周期 {s.get('horizon_days')}天 —— {s.get('reason', '')[:80]}")
    lines += ["", "## 投资组合经理最终意见（TradingAgents）", texts["portfolio_manager"][-3000:],
              "", "## 交易员方案", texts["trader"][-2000:],
              "", "## 条件入场（挂单）字段说明",
              "开仓时机不再局限于每天 08:00。若你认为现价不适合开仓、但在某个价位愿意开仓，请 action=pending 并填写：",
              "- entry_side：long=做多，short=做空；entry_trigger_px：触发价；entry_condition：gte=价格涨到≥触发价时开仓"
              "（做多突破/做空反弹到阻力），lte=跌到≤触发价时开仓（做多回踩/做空跌破支撑）；",
              "- entry_stop_loss：成交后的止损价（做多低于触发价、做空高于触发价，距离≤硬性止损上限）；entry_take_profit：止盈价（可省略）；",
              "- entry_size_pct：保证金（%权益）；leverage：杠杆；entry_expire_hours：有效期（小时，过期自动撤销）；entry_reason：为什么选这个价位。",
              "条件单直接挂在 OKX 模拟盘，由交易所按最新价触发。注意：你只是投票角色之一，最终方向、仓位、入场价与止损由各角色"
              "按历史权重加权聚合决定，投资组合经理只负责执行（过护栏后在 OKX 模拟盘下市价单或挂 trigger 条件单，均附止损止盈）。"
              "仓位由代码按风险定仓（单笔止损亏损上限×共识强度×回撤缩放），size_pct 已废弃不再影响下单；你可以用 risk_scale（0~1，只降不升，0=否决）表达把握程度；"
              "leverage 只作上限。已有持仓时不要填写条件入场。",
              "", "字段一致性（必须遵守，代码以动作标签为准）：action=hold = 观望/不入场（不算多空票，会被视为否决，本币不开仓、撤旧条件单），"
              "此时填写的条件单字段一律忽略；想按条件价位入场时 action=pending 并完整填写 entry_side/entry_trigger_px/entry_condition/entry_stop_loss，"
              "且 entry_size_pct 必须 >0（只作为挂单有效标记，实际仓位按风险定仓）；reduce_fraction 只在 action=reduce 时填写；"
              "invalidation 必填。",
              "已有持仓且 action=hold 或 reduce 时：new_stop 只能收紧止损；new_tp 可把止盈调近或调远，但必须在现价盈利一侧且不会立即触发（距现价至少 "
              f"{float((cfg.get('manager') or dict()).get('tp_min_gap_pct', 0.3)):g}%）。",
              "请给出结构化决策。没有足够把握就 hold；止损请依据结构位/ATR 给出具体价位。理由用中文。"]
    return "\n".join(lines)


def decide_and_execute(cfg, coin, trade_date, fs, st, tracker, source="daily") -> dict:
    inst = coin["inst_id"]
    mk = market_ctx(inst)
    prices = {inst: mk["last"]}
    from desk import demo_exec
    bk = demo_exec.book(cfg, prices) if demo_exec.is_demo(cfg) else ledger.book(cfg, prices)
    pos = next((p for p in bk["positions"] if p["inst_id"] == inst), None)
    w = weights(cfg)
    S = consensus(st, w)          # 交易经理提示词里展示的是不含交易经理本人的共识
    texts = role_texts(fs)
    from desk import sessions
    pre = sessions.did_prefix(trade_date, settings.SESSION)   # 早场 D-<日期>-<币>，晚场 D-<日期>-E-<币>
    did = f"{pre}{inst.split('-')[0]}"
    used = {d["decision_id"] for d in settings.read_jsonl(DECISIONS)}
    k = 2
    while did in used or f"{did}" in used:
        did = f"{pre}{inst.split('-')[0]}-{k}"; k += 1
        if did not in used:
            break
    tok = dllm.STAGE.set(f"{inst}/交易经理")
    try:
        # L5：交易经理也读自己的角色手册（playbooks/trade_manager 当前版本）
        model = roles.PlaybookLLM(dllm.make_llm(cfg, "deep", tracker, role="trade_manager").with_structured_output(ManagerDecision),
                                  "trade_manager")
        dec = model.invoke(_prompt(cfg, coin, mk, pos, bk, st, w, S, texts))
        if isinstance(dec, dict):
            dec = ManagerDecision(**dec)
        absent = False
    except Exception as e:
        # 调用失败：不再用默认中性 0.5 票计入共识，这一票直接不计；保守处理，本次不开新仓（不算否决）
        dec = ManagerDecision(action="hold", confidence=0.0, rationale=f"交易经理调用失败（{type(e).__name__}）：{TM_ABSENT_NOTE}")
        absent = True
    finally:
        dllm.STAGE.reset(tok)

    st = dict(st)
    status = "absent" if absent else tm_status(dec, pos, mk)
    if not absent:
        st["trade_manager"] = tm_stance(dec, mk, pos)
    res = pm_execute(cfg, coin, did, st, w, mk, bk, pos, dec.rationale, source=source, tm=None if absent else dec,
                     tm_state=status)
    action, size, notes, execd, agg = res["action"], res["size_pct"], res["notes"], res["execution"], res["aggregate"]
    S_pre_tm = S   # 交易经理提示里看到的口径（不含其本人投票）
    S = agg["S"]
    apply_new_stop(cfg, pos, dec, action, execd, notes)
    apply_new_tp(cfg, pos, dec, action, execd, notes)

    rec = {"decision_id": did, "date": trade_date, "session": record_session(source), "time": settings.now_str(), "inst_id": inst, "ticker": coin["ticker"],
           "price": mk["last"], "market": mk, "consensus": round(S, 4), "consensus_pre_tm": round(S_pre_tm, 4), "weights": w,
           "ta_signal": fs.get("_signal"), "manager": dec.model_dump(), "final_action": action, "size_pct": size,
           "notes": notes, "execution": execd, "stances": st, "aggregate": agg, "source": source,
           "pending": res.get("pending"), "tm_status": status,
           "tm_label_mismatch": None if absent else tm_label_mismatch(dec.model_dump()),
           "field_missing": None if absent else (tm_field_issues(dec.model_dump())["missing"] or None),
           "stance_field_conflicts": {r: s["field_conflict"] for r, s in st.items() if s.get("field_conflict")} or None,
           "config_hash": settings.config_hash(cfg), "playbook_versions": roles.versions()}   # L1 追溯
    settings.append_jsonl(DECISIONS, rec)
    return rec


def apply_new_stop(cfg, pos, dec, action, execd, notes):
    """交易经理 new_stop：hold 与减仓成功后都执行，只许收紧（多单只上移、空单只下移），放宽由 move_stop 拒绝并写入 notes。
    减仓后按剩余张数挂止损单。"""
    from desk import demo_exec
    reduced = action == "reduce" and isinstance(execd, dict) and execd.get("ok")
    if pos and dec.new_stop and action == "reduce" and not reduced:
        notes.append("减仓未成功，new_stop 未执行")
    if pos and dec.new_stop and (action.startswith("hold") or reduced):
        if reduced and execd.get("remaining") is not None:   # 减仓后止损单张数须等于剩余仓位
            pos = {**pos, "contracts": execd["remaining"]}
        if pos.get("legacy"):
            notes.append("旧纸面仓由旧仓盯盘管理，不改止损")
        elif pos.get("venue") == "okx_demo":
            try:
                ok, msg = demo_exec.move_stop(cfg, pos, float(dec.new_stop))
            except Exception as e:
                msg = f"模拟盘改止损失败：{e}"
            notes.append(msg)
        else:
            ok, msg = ledger.move_stop(cfg, pos["trade_id"], float(dec.new_stop))
            notes.append(msg)


def apply_new_tp(cfg, pos, dec, action, execd, notes):
    """交易经理 new_tp：hold 与减仓成功后都执行（参照 apply_new_stop），改同一张止损止盈单，张数与剩余仓位一致。
    止盈可调近或调远，但须在现价盈利一侧且不立即触发（demo_exec.move_tp 校验）；失败或拒绝写进 notes。"""
    from desk import demo_exec
    tp = getattr(dec, "new_tp", None)
    if not (pos and tp):
        return
    reduced = action == "reduce" and isinstance(execd, dict) and execd.get("ok")
    if action == "reduce" and not reduced:
        notes.append("减仓未成功，new_tp 未执行"); return
    if not (action.startswith("hold") or reduced):
        return
    if reduced and execd.get("remaining") is not None:
        pos = {**pos, "contracts": execd["remaining"]}
    if pos.get("venue") != "okx_demo":
        notes.append("非模拟盘仓位，new_tp 未执行"); return
    try:
        ok, msg = demo_exec.move_tp(cfg, pos, float(tp))
    except Exception as e:
        msg = f"模拟盘改止盈失败：{type(e).__name__}: {e}"
    notes.append(msg)


def pm_execute(cfg, coin, did, st, w, mk, bk, pos, rationale, source="daily", tm=None, tm_state=None) -> dict:
    """统一开仓流程（每日 08:00 与止损后再讨论共用）：各角色立场 → 加权聚合 → 组合经理执行。
    组合经理不投票：只把聚合结果过护栏后，现价下纸面单，或挂/替换条件入场单。
    已有持仓时的卖出/减仓仍按交易经理的决策执行（不属于开仓路径）。"""
    from desk import demo_exec, pending
    if demo_exec.is_demo(cfg):
        return pm_execute_demo(cfg, coin, did, st, w, mk, bk, pos, rationale, source, tm, tm_state)
    inst = coin["inst_id"]
    agg = aggregate(cfg, st, w, mk)
    notes, execd, pend = list(agg["notes"]), None, None
    action, size = "hold", 0.0
    if pos:
        if tm is not None and tm.action == "sell":
            r = ledger.close_position(cfg, pos["trade_id"], "经理决策清仓")
            execd = {"type": "close", "ok": True, "trade_id": r["trade_id"], "px": float(r["exit_px"]), "pnl": float(r["pnl_usd"])}
            action = "sell"
        elif tm is not None and tm.action == "reduce":
            f = _reduce_fraction(tm, notes)
            r = ledger.close_position(cfg, pos["trade_id"], f"经理决策减仓{f:.0%}", fraction=f)
            execd = {"type": "reduce", "ok": True, "trade_id": r["trade_id"], "px": float(r["exit_px"]), "pnl": float(r["pnl_usd"])}
            action = "reduce"
        elif agg["buy"]:
            notes.append("该币已有持仓，不加仓/不反手 → 持有")
        pending.cancel(cfg, inst, "已有持仓")
    elif tm_state in ("veto", "absent"):
        why = TM_VETO_NOTE if tm_state == "veto" else TM_ABSENT_NOTE
        notes.append(why); action = "hold(交易经理否决)" if tm_state == "veto" else "hold(交易经理缺席)"
        if tm_state == "veto":
            pending.cancel(cfg, inst, "交易经理否决")
    elif not agg["buy"]:
        pending.cancel(cfg, inst, "新决策不支持入场")
    else:
        size = agg["size_pct"]
        sl = agg["stop_loss"]
        side = agg["side"]
        sw = ledger.swap_cfg(cfg)
        lev = float(min(max((tm.leverage if tm is not None and tm.leverage else 1), 1), __import__('desk.config_safety', fromlist=['x']).max_open_leverage(cfg)))
        if sl:   # 杠杆过高导致止损晚于强平时自动降杠杆
            while lev > 1 and ((side == "long" and sl <= ledger.liq_price(side, agg["entry_px"], lev, sw["maint_margin_rate"]))
                               or (side == "short" and sl >= ledger.liq_price(side, agg["entry_px"], lev, sw["maint_margin_rate"]))):
                lev = max(1.0, lev - 0.5)
        agg["leverage"] = lev
        if not sl:
            notes.append("各角色没有给出有效止损价，拒绝开仓/挂单"); size = 0.0
        elif size * bk["equity"] / 100 < cfg["guardrails"]["min_order_usd"]:
            notes.append("仓位过小，低于最小下单额"); size = 0.0
        elif agg["condition"] == "now":
            pending.cancel(cfg, inst, "改为现价成交")
            ok, info = ledger.open_position(cfg, inst, size * bk["equity"] / 100 * lev, float(sl), agg["take_profit"],
                                            agg["horizon_days"], did, rationale, bk["equity"], ref_px=mk["last"],
                                            side=side, leverage=lev)
            execd = {"type": "open", "ok": ok, **info}
            action = ("buy" if side == "long" else "short") if ok else "hold(护栏拒单)"
            if not ok:
                notes.append("护栏拒单：" + "；".join(info["reasons"]))
        else:
            exp = (tm.entry_expire_hours if tm is not None and tm.entry_expire_hours else None) \
                or cfg.get("pending", {}).get("default_expire_hours", 24)
            pend = pending.place(cfg, inst, {
                "decision_id": did, "source": source, "side": side, "leverage": lev, "trigger_px": agg["entry_px"], "condition": agg["condition"],
                "stop_loss": sl, "take_profit": agg["take_profit"], "size_pct": size, "horizon_days": agg["horizon_days"],
                "expire_hours": float(exp), "consensus": agg["S"], "ref_px": mk["last"],
                "reason": ((tm.entry_reason if tm is not None else "") or rationale or "")[:200],
                "role_levels": {r: {"entry_px": st[r].get("entry_px"), "condition": st[r].get("entry_condition"),
                                    "stop": st[r].get("stop"), "weight": w[r], "confidence": st[r].get("confidence")}
                                for r in VOTERS if r in st}})
            action = "pending"
            notes.append(f"挂{'多' if side == 'long' else '空'}单 {lev:g}x：{'≥' if agg['condition'] == 'gte' else '≤'} {agg['entry_px']:.6g}，止损 {sl:.6g}，"
                         f"仓位 {size:.1f}%，{float(exp):.0f} 小时有效")
    return {"action": action, "size_pct": size, "notes": notes, "execution": execd, "aggregate": agg, "pending": pend}


def _levfix(cfg, agg, tm):
    sl, side = agg["stop_loss"], agg["side"]
    sw = ledger.swap_cfg(cfg)
    lev = float(min(max((tm.leverage if tm is not None and tm.leverage else 1), 1), __import__('desk.config_safety', fromlist=['x']).max_open_leverage(cfg)))
    if sl:   # 杠杆过高导致止损晚于强平时自动降杠杆
        while lev > 1 and ((side == "long" and sl <= ledger.liq_price(side, agg["entry_px"], lev, sw["maint_margin_rate"]))
                           or (side == "short" and sl >= ledger.liq_price(side, agg["entry_px"], lev, sw["maint_margin_rate"]))):
            lev = max(1.0, lev - 0.5)
    return lev


def demo_size(cfg, agg, mk, bk, tm, inst) -> dict:
    """R1/R3/R4：按风险定仓。返回 sizing.size 的结果，附 room/limit 与 exposure。"""
    from desk import sizing, config_safety, equity_guard
    from desk import demo_exec as DX
    rc, M, sw = sizing.rcfg(cfg), cfg["manager"], ledger.swap_cfg(cfg)
    eq = float(bk["live_equity"])
    side, entry, stop = agg["side"], float(agg["entry_px"]), float(agg["stop_loss"])
    bk2 = {**bk, "trigger_risk": DX.trigger_risk(cfg, [t for t in bk.get("triggers") or [] if t["instId"] != inst])}
    ex = sizing.book_exposure(bk2, rc)
    dist = abs(entry - stop) / entry + float(rc["cost_buffer_pct"])
    room, lim = sizing.headroom(rc, eq, ex, side, dist)
    gs = equity_guard.status(cfg, eq)
    if gs.get("unavailable"):   # 回撤数据读不到 → 拒绝（fail-closed）
        return {"ok": False, "reason": "guard_data_unavailable", "clipped": []}
    dd = max(0.0, 1 - eq / float(gs["peak_equity"])) if gs.get("peak_equity") else 0.0
    r = sizing.size(rc, equity=eq, S=float(agg["S"]), entry=entry, stop=stop, side=side, full_size=float(M.get("full_size_consensus", 0.6)),
                    half_below=float(M["half_size_below"]), lev_cap=config_safety.max_open_leverage(cfg),
                    mmr=float(sw["maint_margin_rate"]), atr=float(mk.get("atr14") or 0), drawdown=dd,
                    max_drawdown=float(equity_guard.risk_cfg(cfg)["max_drawdown_pct"]),
                    tm_lev_cap=(tm.leverage if tm is not None and getattr(tm, "leverage", None) else None),
                    tm_risk_scale=(float(tm.risk_scale) if tm is not None and getattr(tm, "risk_scale", None) is not None else 1.0),
                    room=room, room_limit=lim, min_order_usd=float(cfg["guardrails"].get("min_order_usd", 0)))
    r.update(exposure={k: round(v, 2) for k, v in ex.items()}, room=round(room, 2), room_limit=lim, equity=eq, drawdown=round(dd, 4))
    return r


def pm_execute_demo(cfg, coin, did, st, w, mk, bk, pos, rationale, source="daily", tm=None, tm_state=None) -> dict:
    """R8：持每币执行锁，锁内重新读模拟盘账户（持仓/挂单/在途子单）后再执行；锁等待超时或重读失败 → 不执行。"""
    from desk import demo_exec as DX, exec_lock
    inst = coin["inst_id"]
    try:
        with exec_lock.hold(inst, float(DX.ecfg(cfg).get("exec_lock_timeout_s", 120)), why=f"pm_execute_demo {did}"):
            bk = DX.book(cfg)
            pos = next((p for p in bk["positions"] if p["inst_id"] == inst), None)
            return _pm_execute_demo_locked(cfg, coin, did, st, w, mk, bk, pos, rationale, source, tm, tm_state)
    except Exception as e:
        from desk import okx_demo
        okx_demo.log_event("exec_error", inst_id=inst, decision_id=did, error=f"{type(e).__name__}: {e}"[:300])
        return {"action": "hold(执行失败)", "size_pct": 0.0, "notes": [f"模拟盘执行失败：{type(e).__name__}: {e}"], "execution": None,
                "aggregate": aggregate(cfg, st, w, mk), "pending": None}


def _cooldown(cfg, inst, agg, did, notes) -> bool:
    """L6：止损后同向冷却。命中时记事件 cooldown_blocked 并写进 notes。"""
    from desk import stop_cooldown, okx_demo
    b = stop_cooldown.blocked(cfg, inst, agg["side"])
    if not b:
        return False
    okx_demo.log_event("cooldown_blocked", inst_id=inst, decision_id=did, **b)
    notes.append(f"止损冷却：{inst} {'多' if b['side'] == 'long' else '空'}单 {b['since_h']} 小时前被止损，"
                 f"冷却还剩 {b['left_h']} 小时，同向不开仓")
    return True


def record_session(source) -> str:
    """决策记录的场次：止盈/止损后再讨论记 rediscuss（不再误记为 morning），其余按当前场次。"""
    return "rediscuss" if str(source).startswith(("post_tp", "post_stop")) else settings.SESSION


def post_tp_override(source, tm, tm_state, agg, notes) -> bool:
    """止盈后再讨论（source=post_tp:*）专用：聚合结果为现价开仓，而交易经理投的是挂条件单或未投现价单时，不现价开仓。
    交易经理给了完整条件单点位 → 改按其入场价、触发条件、止损、止盈挂条件单（走原条件单流程与护栏），返回 False；
    未给完整点位 → 返回 True（观望不下单），并写 notes。止损后再讨论与早晚场不受影响（返回 False，agg 不变）。"""
    if not str(source).startswith("post_tp") or tm is None or tm_state != "go":
        return False
    if not (agg.get("buy") and agg.get("condition") == "now") or tm.action in ("buy", "short", "sell", "reduce"):
        return False
    if tm.action == "pending" and pending_complete(tm):
        side = "long" if tm.entry_side == "long" else "short"
        if side != agg.get("side"):
            notes.append("止盈后再讨论：交易经理条件单方向与共识相反 → 观望不下单")
            return True
        agg.update({"side": side, "entry_px": float(tm.entry_trigger_px), "condition": tm.entry_condition,
                    "stop_loss": float(tm.entry_stop_loss),
                    "take_profit": float(tm.entry_take_profit) if tm.entry_take_profit else None,
                    "plan_group": "post_tp_tm_pending"})
        notes.append(f"止盈后再讨论：交易经理投挂条件单、否决现价追单 → 不现价开仓，改按交易经理点位挂条件单"
                     f"（{'≥' if tm.entry_condition == 'gte' else '≤'} {float(tm.entry_trigger_px):g}，止损 {float(tm.entry_stop_loss):g}，"
                     f"止盈 {tm.entry_take_profit if tm.entry_take_profit else '无'}）")
        return False
    notes.append("止盈后再讨论：交易经理未投现价开仓且未给完整条件单点位 → 观望不下单")
    return True


def pre_trade_gate(cfg, inst, mk) -> str | None:
    """D1 第 1–3 点：执行前复核。返回拒绝事件名（并已记事件），通过返回 None。"""
    from desk import okx_demo, sizing
    if okx_data.fallback_used(inst):
        okx_demo.log_event("data_fallback_no_trade", inst_id=inst)
        return "data_fallback_no_trade"
    bd = mk.get("bar_date")
    if bd:
        import datetime as _d
        age = (_d.datetime.now(_d.timezone.utc).date() - _d.date.fromisoformat(bd)).days
        if age > int(sizing.rcfg(cfg)["max_daily_bar_age_days"]):
            okx_demo.log_event("data_stale_no_trade", inst_id=inst, bar_date=bd, age_days=age)
            return "data_stale_no_trade"
    atr = mk.get("atr14")
    if atr:
        now_px = okx_data.last_price(inst)
        lim = float(sizing.rcfg(cfg)["max_price_drift_atr"]) * float(atr)
        if abs(now_px - float(mk["last"])) > lim:
            okx_demo.log_event("price_drift_skip", inst_id=inst, analysis_px=mk["last"], now_px=now_px, limit=round(lim, 6))
            return "price_drift_skip"
    return None


def _pm_execute_demo_locked(cfg, coin, did, st, w, mk, bk, pos, rationale, source="daily", tm=None, tm_state=None) -> dict:
    """OKX 模拟盘直连版组合经理执行（禁止实盘，见 desk/demo_exec.py）：聚合 → 护栏（模拟盘数据）→ 现价市价单
    或 OKX trigger 条件单（均附带止损止盈）。不写本地纸面账本 / 本地 pending。任何模拟盘错误只记入 notes 与 demo_events。"""
    from desk import demo_exec as DX
    inst = coin["inst_id"]
    agg = aggregate(cfg, st, w, mk)
    notes, execd, pend = list(agg["notes"]), None, None
    action, size = "hold", 0.0
    cancel_ok = DX.ecfg(cfg)["cancel_pending_on_new_decision"]
    tp_hold = post_tp_override(source, tm, tm_state, agg, notes)
    try:
        if pos and pos.get("legacy"):
            notes.append("该币是旧纸面仓（由旧仓盯盘管理止损/止盈/强平），本流程不操作、不加仓")
        elif pos:
            if tm is not None and tm.action == "sell":
                execd = DX.close(cfg, pos, "经理决策清仓"); action = "sell"
            elif tm is not None and tm.action == "reduce":
                f = _reduce_fraction(tm, notes)
                execd = DX.reduce(cfg, pos, f, f"经理决策减仓{f:.0%}"); action = "reduce"
            elif agg["buy"]:
                notes.append("该币已有模拟盘持仓，不加仓/不反手 → 持有")
            if cancel_ok:
                DX.cancel_triggers(cfg, inst, "已有持仓")
        elif tm_state in ("veto", "absent"):
            # 交易经理否决/缺席：不下现价单、不挂条件单；按 cancel_pending_on_new_decision 撤该币旧条件单（受保护的不撤）
            notes.append(TM_VETO_NOTE if tm_state == "veto" else TM_ABSENT_NOTE)
            action = "hold(交易经理否决)" if tm_state == "veto" else "hold(交易经理缺席)"
            # R9：缺席与否决一致，都撤该币未受保护的条件单（缺席原因 tm_absent）
            if cancel_ok:
                why = "交易经理否决" if tm_state == "veto" else "tm_absent"
                gone = DX.cancel_triggers(cfg, inst, why)
                if gone:
                    notes.append(("交易经理否决" if tm_state == "veto" else "交易经理缺席(tm_absent)") + "，撤旧条件单 " + "、".join(gone))
        elif tp_hold:
            action = "hold(止盈后不追单)"
            if cancel_ok:
                DX.cancel_triggers(cfg, inst, "止盈后不追单")
        elif not agg["buy"]:
            if agg.get("plan_conflict"):
                from desk import okx_demo
                okx_demo.log_event("plan_conflict", inst_id=inst, decision_id=did, scores=agg.get("plan_scores"))
            if cancel_ok:
                DX.cancel_triggers(cfg, inst, "新决策不支持入场")
        elif _cooldown(cfg, inst, agg, did, notes):
            action = "hold(止损冷却)"
            if cancel_ok:
                DX.cancel_triggers(cfg, inst, "止损冷却")
        else:
            size, sl, side = 0.0, agg["stop_loss"], agg["side"]
            lev = agg["leverage"] = 1.0
            gate = pre_trade_gate(cfg, inst, mk)
            if gate:
                sz_info = {"ok": False, "reason": gate}
                notes.append(f"执行前复核未通过（{gate}），本次不开仓")
            else:
                sz_info = demo_size(cfg, agg, mk, bk, tm, inst) if sl else {"ok": False, "reason": "no_stop"}
            agg["sizing"] = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in sz_info.items()}
            from desk import okx_demo
            for c in sz_info.get("clipped") or []:
                okx_demo.log_event("size_clipped", inst_id=inst, decision_id=did, limit=c["limit"],
                                   notional_before=round(c["before"], 2), notional_after=round(c["after"], 2))
            if sz_info["ok"]:
                lev = agg["leverage"] = sz_info["leverage"]
                size = sz_info["margin"] / bk["equity"] * 100   # 交给执行层的保证金%（equity_basis=live 时与实时权益一致）
                agg["size_pct"] = round(size, 4)
                notes.append(f"按风险定仓：风险预算 {sz_info['risk_usd']:.2f}，名义 {sz_info['notional']:.2f}，杠杆 {lev:g}x，"
                             f"保证金 {sz_info['margin']:.2f}，止损亏损 {sz_info['stop_loss_usd']:.2f}")
            if not sl:
                notes.append("没有有效止损价，拒绝开仓/挂单")
            elif not sz_info["ok"] and not gate:
                okx_demo.log_event(sz_info["reason"], inst_id=inst, decision_id=did, side=side, entry=agg["entry_px"], stop=sl)
                notes.append(f"定仓拒绝：{sz_info['reason']}"); size = 0.0
            elif agg["condition"] == "now":
                DX.cancel_triggers(cfg, inst, "改为现价成交")
                ok, info = DX.open_now(cfg, bk, inst, size, float(sl), agg["take_profit"], side, lev, did,
                                       agg["horizon_days"], mk["last"])
                execd = {"type": "open", "ok": ok, **info}
                action = ("buy" if side == "long" else "short") if ok else "hold(护栏拒单)"
                if not ok:
                    notes.append("护栏拒单：" + "；".join(info["reasons"]))
            else:
                exp = (tm.entry_expire_hours if tm is not None and tm.entry_expire_hours else None) \
                    or cfg.get("pending", {}).get("default_expire_hours", 24)
                ok, info = DX.place_trigger(cfg, bk, inst, {
                    "decision_id": did, "source": source, "side": side, "leverage": lev, "trigger_px": agg["entry_px"],
                    "condition": agg["condition"], "stop_loss": sl, "take_profit": agg["take_profit"], "size_pct": size,
                    "horizon_days": agg["horizon_days"], "expire_hours": float(exp), "consensus": agg["S"],
                    "reason": ((tm.entry_reason if tm is not None else "") or rationale or "")[:200]})
                if ok:
                    pend = info; action = "pending"
                    notes.append(f"模拟盘条件单 {info['algoId']}：{'多' if side == 'long' else '空'} {lev:g}x "
                                 f"{'≥' if agg['condition'] == 'gte' else '≤'} {agg['entry_px']:.6g}，止损 {sl:.6g}，"
                                 f"{info['sz']} 张，{float(exp):.0f} 小时有效")
                else:
                    action = "hold(护栏拒单)"; notes.append("护栏拒单：" + "；".join(info["reasons"]))
    except Exception as e:
        from desk import okx_demo
        okx_demo.log_event("exec_error", inst_id=inst, decision_id=did, error=str(e))
        notes.append(f"模拟盘执行失败：{type(e).__name__}: {e}")
        action = "hold(执行失败)" if action == "hold" else action
    return {"action": action, "size_pct": size, "notes": notes, "execution": execd, "aggregate": agg, "pending": pend}
