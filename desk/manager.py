"""交易经理：LLM（深度模型）给出结构化决策，代码层做角色加权共识、仓位规则与硬护栏，然后执行（execution.venue=okx_demo 时直连 OKX 模拟盘，禁止实盘；paper 时纸面执行）。"""
from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, Field

from desk import add_position, drift, ledger, okx_data, roles, settings
from desk import llm as dllm
from desk.roles import INIT_WEIGHT, ROLE_CN, ROLE_KEYS
from desk.stances import INVALID_STATUS, normalize as _norm_stance, role_texts, stance_status

WEIGHTS = settings.STATE / "weights.json"
DECISIONS = settings.STATE / "decisions.jsonl"
# "close" = 交易经理对已有持仓平仓/减仓：是仓位管理动作，不是对未来价格方向的预测 → 共识里记 0，不参与方向打分
DIR_NUM = {"bullish": 1, "bearish": -1, "neutral": 0, "close": 0}
ACT_CN = {"hold(交易经理否决)": "不入场（交易经理否决）", "hold(交易经理缺席)": "不开新仓（交易经理缺席）", "buy": "开多", "short": "开空", "hold": "持有/观望", "sell": "平仓", "reduce": "减仓", "pending": "挂条件入场单", "hold(止损冷却)": "不开仓（止损冷却）", "hold(止盈后不追单)": "不开仓（止盈后交易经理否决追单）",
          "add_long": "同向加多", "add_short": "同向加空", "pending_add": "挂条件加仓单", "hold(价格漂移)": "不下单（价格漂移超阈值）"}


# 投票角色：组合经理只负责执行（过护栏、在 OKX 模拟盘下市价单/挂条件单），不投票、不计权重；交易经理作为一个投票角色
VOTERS = [r for r in ROLE_KEYS if r != "portfolio_manager"] + ["trade_manager"]
TM_INIT_WEIGHT = 1.0


def _mcfg(cfg) -> dict:
    return (cfg or {}).get("manager") or {}


def tm_no_vote(cfg) -> bool:
    """2026-10-30 方案一（manager.tm_no_vote_weight，默认 true）：交易经理不再作为投票角色计入加权共识 S，
    也不参与权重学习；方向由其余投票角色的加权共识决定，交易经理只能否决或定点位，不能翻转多空。false=旧行为。"""
    return bool(_mcfg(cfg).get("tm_no_vote_weight", True))


def tm_decides_entry(cfg) -> bool:
    """2026-10-30 方案一（manager.tm_decides_entry，默认 true）：入场价/条件单点位/止损/止盈只由交易经理定，
    各角色的 entry_px 与「对现有挂单」字段只作参考，不再参与入场分组评分。false=回到 choose_plan 分组。"""
    return bool(_mcfg(cfg).get("tm_decides_entry", True))


def tm_required_for_open(cfg) -> bool:
    """manager.tm_required_for_open（默认 true，2026-10-30 用户决定）：最后由交易经理拍板（职能等同原项目组合经理）。
    交易经理没有生成有效的同向开仓结论（缺席/调用失败/空输出/解析失败，或没给同向的现价单或完整条件单）→ 本次不开仓、不加仓，
    不再回退到现价 + ATR+2R 基准开仓。false=旧行为（tm_entry_plan 回退为现价入场 + ATR+2R 基准）。"""
    return bool(_mcfg(cfg).get("tm_required_for_open", True))


def voters(cfg=None) -> list:
    return [r for r in VOTERS if r != "trade_manager"] if tm_no_vote(cfg if cfg is not None else settings.load_config()) else list(VOTERS)


def weights(cfg=None) -> dict:
    """决策用权重。learning.apply_weights=false（学习冻结）时一律用初始权重，学到的权重只在影子文件里。"""
    cfg = cfg if cfg is not None else settings.load_config()
    w = settings.read_json(WEIGHTS, {}) if (cfg.get("learning") or {}).get("apply_weights", False) else {}
    # 只给投票角色权重：组合经理只执行、不投票，不在权重里
    out = {r: float(w.get(r, INIT_WEIGHT[r])) for r in ROLE_KEYS if r != "portfolio_manager"}
    if not tm_no_vote(cfg):   # 方案一：交易经理不投票、不计权重（权重文件里旧的 trade_manager 记录忽略）
        out["trade_manager"] = float(w.get("trade_manager", TM_INIT_WEIGHT))
    return out


def valid_voters(st: dict, w: dict | None = None) -> list:
    """L3：有效投票角色（排除 failed/absent/data_missing）。给了 w 时只算 w 里有权重的角色（方案一：交易经理不在 w 里 → 不计票）。"""
    return [r for r in VOTERS if r in st and stance_status(st[r]) not in INVALID_STATUS and (w is None or r in w)]


def consensus(st: dict, w: dict) -> float:
    rs = valid_voters(st, w)
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
TM_NO_CONCLUSION_NOTE = "交易经理无结论，不开仓"


def tm_entry_plan(cfg, tm, side: str, last: float, atr) -> dict:
    """方案一（manager.tm_decides_entry）：入场方案只由交易经理定。返回 {group, entry, condition, stop, tp, horizon, notes}。
    1) 交易经理 action=pending 且点位完整、方向与共识一致 → 按其触发价/条件/止损/止盈挂条件单（group=tm_pending）；
    2) 交易经理 action=buy/short 且方向与共识一致 → 现价入场，止损止盈用交易经理给的（group=tm）；
    3) 其他（交易经理缺席、没给完整点位、方向不一致）→ 回退：现价入场 + ATR+2R 基准止损止盈（group=tm_fallback_now），
       不再用各角色 entry_px 分组，也不用各角色止损止盈加权；交易经理缺席/否决时上层本来就不开新仓。
    交易经理给的止损若在错误一侧或距离超过 risk.stop_max_pct，弃用并回退 ATR 基准；止盈在错误一侧弃用。RR≥min_rr 由 finalize_sl_tp 保证。"""
    from desk import sizing
    rc = sizing.rcfg(cfg)
    sg = 1 if side == "long" else -1
    notes: list[str] = []
    out = {"group": "tm_fallback_now", "entry": last, "condition": "now", "stop": None, "tp": None,
           "horizon": float(getattr(tm, "horizon_days", None) or 5) if tm is not None else None, "notes": notes}
    act = getattr(tm, "action", None)
    if tm is not None and act == "pending" and pending_complete(tm) and ("long" if tm.entry_side == "long" else "short") == side:
        out.update(group="tm_pending", entry=float(tm.entry_trigger_px), condition=tm.entry_condition,
                   stop=tm.entry_stop_loss, tp=tm.entry_take_profit)
        notes.append(f"入场方案以交易经理为准：挂条件单 {'≥' if tm.entry_condition == 'gte' else '≤'} {float(tm.entry_trigger_px):g}")
    elif tm is not None and act in ("buy", "short") and ("long" if act == "buy" else "short") == side:
        out.update(group="tm", stop=tm.stop_loss, tp=tm.take_profit)
        notes.append("入场方案以交易经理为准：现价入场")
    else:
        notes.append("交易经理未给出与共识同向的完整入场点位：回退为现价入场，止损止盈按 ATR+2R 基准（不用各角色入场价）")
        return out
    e = float(out["entry"])
    try:
        stp = float(out["stop"]) if out["stop"] not in (None, "") else None
    except (TypeError, ValueError):
        stp = None
    if stp is not None and (sg * (e - stp) <= 0 or abs(e - stp) / e > float(rc["stop_max_pct"]) + 1e-12):
        notes.append(f"交易经理止损 {stp:g} 在错误一侧或距离超过上限 {float(rc['stop_max_pct']):.1%}，弃用，改用 ATR 基准止损")
        stp = None
    try:
        tpv = float(out["tp"]) if out["tp"] not in (None, "") else None
    except (TypeError, ValueError):
        tpv = None
    if tpv is not None and sg * (tpv - e) <= 0:
        notes.append(f"交易经理止盈 {tpv:g} 不在盈利一侧，弃用，改用 ATR 基准止盈")
        tpv = None
    out.update(stop=stp, tp=tpv)
    return out


def _agg(cfg, st, w, mk, ex, tm):
    """开关关闭时保持旧调用签名（不传 tm）。"""
    kw = {"existing": ex} if ex else {}
    if tm_decides_entry(cfg):
        kw["tm"] = tm
    return aggregate(cfg, st, w, mk, **kw)


def aggregate(cfg, st: dict, w: dict, mk: dict, existing: dict | None = None, tm=None) -> dict:
    """按角色权重×置信度聚合：方向（共识 S）、仓位、入场价位与条件、止损、止盈、周期。"""
    M, P = cfg["manager"], cfg.get("pending", {})
    st = {r: _norm_stance(s) for r, s in st.items()}   # B1：中性/平仓立场的价位字段不参与聚合
    S = consensus(st, w)
    last = mk["last"]
    th = float(M.get("consensus_threshold", 0.1))
    side = "long" if S > th else ("short" if S < -th else None)
    want = "bullish" if side != "short" else "bearish"
    sg = 1 if side != "short" else -1
    nv, need = len(valid_voters(st, w)), int(M.get("min_valid_voters", 0) or 0)
    if need and nv < need:   # L3：有效票不足 → 不交易
        side = None
    bulls = [r for r in valid_voters(st, w) if st[r]["direction"] == want and (st[r].get("confidence") or 0) > 0]

    def wavg(vals):
        vals = [(w[r] * float(st[r]["confidence"]), v) for r, v in vals if v]
        den = sum(a for a, _ in vals)
        return sum(a * v for a, v in vals) / den if den else None
    band = float(P.get("immediate_band_pct", 0.3)) / 100
    from desk import sizing
    rc = sizing.rcfg(cfg)
    notes = []
    sc = sizing.scfg(cfg)
    # 改动 2：同向现有挂单（无持仓路径由调用方传入），没写入场价的同向角色默认「保留」
    ex = existing if (sc["pending_action_field"] and existing and side and existing.get("side") == side
                      and existing.get("trigger_px")) else None
    tmp = None
    if tm_decides_entry(cfg):
        # 方案一：入场方案只由交易经理定；各角色 entry_px / 对现有挂单字段只作参考，不参与分组评分
        ex = None
        tmp = tm_entry_plan(cfg, tm, side or "long", last, mk.get("atr14")) if bulls else None
        plan = ({"group": tmp["group"], "conflict": False, "scores": None, "entry": tmp["entry"], "condition": tmp["condition"],
                 "horizon": tmp["horizon"]} if tmp else {"group": None, "conflict": False, "scores": {}})
        if tmp and side:
            if tmp["group"] == "tm_fallback_now" and tm_required_for_open(cfg):
                # 交易经理拍板：没有同向有效入场结论 → 不开仓、不加仓（不回退现价 + ATR+2R 基准）
                notes.append("交易经理无同向有效入场结论（未给同向现价单或完整条件单），不开仓（manager.tm_required_for_open）")
                plan = {"group": None, "conflict": False, "scores": {}, "tm_no_conclusion": True}
            else:
                notes.extend(tmp["notes"])
    else:
        plan = sizing.choose_plan(st, w, bulls, side or "long", last, band, float(rc["plan_conflict_pct"]),
                                  no_entry_not_now=bool(sc["no_entry_not_now_vote"]), existing=ex) if bulls else {"group": None, "conflict": False, "scores": {}}
    conflict = bool(side and plan["conflict"])
    entry, cond = (plan.get("entry") or last), plan.get("condition") or "now"
    atr = mk.get("atr14")
    min_rr = float(rc.get("min_rr_after_adjust", 1.5))
    levels_meta = {"rr": None, "adjusted": False, "baseline_stop": None, "baseline_tp": None}
    if atr:   # R2：先 ATR+2R 基准，再同向角色 stop/target 加权调整，最后 RR≥min_rr
        base_stop, base_tp, _d = sizing.stop_take(side or "long", entry, float(atr), rc)
        levels_meta["baseline_stop"], levels_meta["baseline_tp"] = base_stop, base_tp
        if tmp is not None:   # 方案一：止损止盈只取交易经理给的（已校验侧向/距离），没给的用 ATR 基准，不再按角色加权
            lv = {"stop": tmp["stop"], "target": tmp["tp"]}
        else:
            lv = sizing.wavg_levels(st, w, bulls, side or "long", entry) if bulls else {"stop": None, "target": None}
        ao = sizing.anchor_opts(cfg, rc, atr, entry)
        if cond != "now":
            # 条件单：点位是为该触发价设计的，保持原逻辑（不收紧/不封顶），只用触发价做基准校验并记录
            if ao["anchor_mult"] and lv.get("stop") is not None and ao["base_dist"] \
                    and abs(entry - float(lv["stop"])) > ao["anchor_mult"] * ao["base_dist"] + 1e-9:
                notes.append(f"条件单止损 {float(lv['stop']):.6g} 距触发价 {abs(entry - float(lv['stop'])) / entry:.2%}，"
                             f"超过基准距离 {ao['base_dist'] / entry:.2%} 的 {ao['anchor_mult']:g} 倍（仅校验记录，按交易经理点位）")
            ao = {}
        fin = sizing.finalize_sl_tp(side or "long", entry, base_stop, base_tp, lv.get("stop"), lv.get("target"), min_rr, **ao)
        stop, tp = fin["stop"], fin["take_profit"]
        levels_meta.update(rr=fin["rr"], adjusted=fin["adjusted"], rr_short=fin.get("rr_short"))
        notes.extend(fin["notes"])
    elif tmp is not None:   # 无 ATR + 方案一：只用交易经理的点位
        stop, tp = tmp["stop"], tmp["tp"]
        levels_meta["rr"] = sizing.rr_ratio(side or "long", entry, stop, tp) if stop and tp else None
    else:     # 无 ATR（仅旧测试/纸面路径）：沿用角色加权
        stop = wavg([(r, float(st[r]["stop"])) for r in bulls if st[r].get("stop") and sg * (entry - float(st[r]["stop"])) > 0])
        tp = wavg([(r, float(st[r]["target"])) for r in bulls if st[r].get("target") and sg * (float(st[r]["target"]) - entry) > 0])
        levels_meta["rr"] = sizing.rr_ratio(side or "long", entry, stop, tp) if stop and tp else None
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
            "tm_no_conclusion": plan.get("tm_no_conclusion") or None,
            "plan_group": plan.get("group"), "plan_scores": plan.get("scores"), "plan_conflict": conflict, "valid_voters": nv,
            "plan_members": plan.get("members"), "plan_direction_only": plan.get("direction_only") or None,
            "plan_implied_keep": plan.get("implied_keep") or None, "plan_cancel_votes": plan.get("cancel_votes") or None,
            "plan_no_entry_fallback_now": plan.get("no_entry_fallback_now") or None,
            "existing_trigger_px": (ex or {}).get("trigger_px"), "entry_by_tm": tmp is not None,
            "rr": levels_meta.get("rr"), "levels_adjusted": levels_meta.get("adjusted"), "rr_short": levels_meta.get("rr_short"),
            "baseline_stop": levels_meta.get("baseline_stop"), "baseline_tp": levels_meta.get("baseline_tp")}


def market_ctx(inst: str) -> dict:
    d = okx_data.candles(inst, "1Dutc", 60)
    c, h, l = d.Close, d.High, d.Low
    tr = (h - l).combine((h - c.shift()).abs(), max).combine((l - c.shift()).abs(), max)
    return {"last": okx_data.last_price(inst), "close": float(c.iloc[-1]), "bar_date": str(d.Date.iloc[-1].date()),
            "ret_5d": float(c.iloc[-1] / c.iloc[-6] - 1) * 100, "ret_20d": float(c.iloc[-1] / c.iloc[-21] - 1) * 100,
            "atr14": float(tr.tail(14).mean()), "hi_20": float(h.tail(20).max()), "lo_20": float(l.tail(20).min())}


def _prompt(cfg, coin, mk, pos, bk, st, w, S, texts, preview=None):
    g = cfg["guardrails"]
    lines = [f"你是加密货币交易台的交易经理，负责对 {coin['name']}（OKX 永续 {coin['inst_id']}）做今日最终执行决策。",
             f"标的是 OKX USDT 永续合约（OKX 模拟盘 Demo Trading 执行，逐仓），可以做多也可以做空，新开仓杠杆上限 {__import__('desk.config_safety', fromlist=['x']).max_open_leverage(cfg):g} 倍（代码强制）。",
             f"硬性规则（代码会强制，违反会被拒单）：单笔保证金≤权益 {g['max_position_pct']}%，总名义≤权益 {g.get('max_total_notional_pct') or g['max_total_exposure_pct'] * ledger.swap_cfg(cfg)['leverage_max']:g}%，"
             f"开仓必须带止损（做多在下方、做空在上方）且止损距离≤{g['max_stop_loss_pct']}%，止损必须先于强平价触发，止损只能收紧，" + ("每币一仓；同向加仓允许（加仓腿独立止盈止损，原仓不动；已有风险+加仓风险≤单笔上限，不足按剩余削减；反向不反手须先平仓）。" if add_position.enabled(cfg) else "每币一仓不加仓。"),
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
    if pos and add_position.enabled(cfg):
        lines += ["", "## 加仓预览（代码按加仓规则预算，只是信息）",
                  describe_add_preview(preview) if preview else "加仓预览：未能计算（无数据），不影响决策流程。",
                  "判断是否加仓时，不要拿原仓止损止盈或原仓均价去类比（例如「离原仓止损仅 N 点」不是否决加仓的理由）："
                  "加仓是新订单，该腿按加仓入场价独立设止损止盈、只覆盖加仓张数，原仓点位不动；请看上面加仓预览里该腿自己的止损距离、RR 与预算。"
                  "最终是否加仓仍由你决定，你的否决权不变（hold/risk_scale=0）。"]
    from desk import analysis as _an
    lines += ["", _an.pending_brief(bk)]
    if tm_no_vote(cfg):
        lines += ["", f"## 各角色观点（权重为历史表现给出；加权共识 S={S:+.3f}，范围 -1~+1）",
                  f"你的角色：你不投票，权重不计。S={S:+.3f} 是全部投票角色（不含你）按权重×置信度汇总的加权共识，也就是代码最终使用的共识，"
                  "方向（做多/做空/不交易）由它决定。你是最终定点位的人：综合下面所有角色意见和行情账户数据拟定这一单，"
                  "决定做不做（不做请 action=hold 或 risk_scale=0，即否决）、入场价或条件单触发价、止损、止盈，以及 risk_scale（0~1，只降不升）。"
                  "你不能翻转多空：与共识方向相反的开仓或挂单会被视为否决。各角色给的入场价只作参考，入场方案以你给出的为准；"
                  "你没给完整点位时，代码按现价入场并用 ATR+2R 基准止损止盈。这一单的盈亏记在你名下。"]
    else:
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
              "条件单直接挂在 OKX 模拟盘，由交易所按最新价触发。"
              + ("注意：方向由全部投票角色的加权共识决定，入场价、条件单点位、止损、止盈由你定（过代码护栏：止损只收紧、RR≥1.5、止损距离上限、价格漂移检查）；"
                 "投资组合经理只负责执行（过护栏后在 OKX 模拟盘下市价单或挂 trigger 条件单，均附止损止盈）。"
                 if tm_no_vote(cfg) else
                 "注意：你只是投票角色之一，最终方向、仓位、入场价与止损由各角色"
                 "按历史权重加权聚合决定，投资组合经理只负责执行（过护栏后在 OKX 模拟盘下市价单或挂 trigger 条件单，均附止损止盈）。")
              + "仓位由代码按风险定仓（单笔止损亏损上限×共识强度×回撤缩放），size_pct 已废弃不再影响下单；你可以用 risk_scale（0~1，只降不升，0=否决）表达把握程度；"
              "leverage 只作上限。" + ("已有持仓时：同向加仓用 buy/short（现价）或 pending（条件加仓，entry_side 须与持仓同向），"
                                    "加仓腿按加仓入场价独立挂止盈止损（原仓止盈止损不动）；不同意加仓请 action=hold 并在理由中写明（共识太弱或预算已满等）。"
                                    if add_position.enabled(cfg) else "已有持仓时不要填写条件入场。"),
              "", "字段一致性（必须遵守，代码以动作标签为准）：action=hold = 观望/不入场（不算多空票，会被视为否决，本币不开仓、撤旧条件单），"
              "此时填写的条件单字段一律忽略；想按条件价位入场时 action=pending 并完整填写 entry_side/entry_trigger_px/entry_condition/entry_stop_loss，"
              "且 entry_size_pct 必须 >0（只作为挂单有效标记，实际仓位按风险定仓）；reduce_fraction 只在 action=reduce 时填写；"
              "invalidation 必填。",
              "已有持仓且 action=hold 或 reduce 时：调点与开仓同一套——先 ATR+2R 基准再全员加权；你可用 new_stop/new_tp 做最终微调，"
              "但 new_stop 只能收紧、new_tp 须在现价盈利一侧且不会立即触发（距现价至少 "
              f"{float((cfg.get('manager') or dict()).get('tp_min_gap_pct', 0.3)):g}%），且调整后盈亏比 RR（止盈距/止损距，相对均价）不得低于 "
              f"{float((cfg.get('risk') or dict()).get('min_rr_after_adjust', 1.5)):g}；你保留否决权（hold/risk_scale=0）。",
              "请给出结构化决策。没有足够把握就 hold；止损请依据结构位/ATR 给出具体价位。理由用中文。"]
    return "\n".join(lines)


def tm_attribution(cfg) -> bool:
    """ledger.tm_attribution（默认 true）：决策记录写 decided_by/decision_type，平仓写交易经理账本，日报列交易经理战绩。"""
    return bool(((cfg or {}).get("ledger") or {}).get("tm_attribution", True))


DTYPE = {"buy": "开仓", "short": "开仓", "add_long": "加仓", "add_short": "加仓", "pending_add": "加仓", "pending": "挂条件单",
         "sell": "平仓", "reduce": "减仓"}


def decision_type(action, dec, pos, absent=False) -> str:
    """这一单的类型：开仓、加仓、持有、平仓、减仓、改点位、挂条件单（pending_add 记为加仓）。"""
    a = str(action)
    if a in DTYPE:
        return DTYPE[a]
    if pos and not absent and (getattr(dec, "new_stop", None) is not None or getattr(dec, "new_tp", None) is not None):
        return "改点位"
    return "持有"


def tm_direction_gate(cfg, S, dec, status, absent, mk, pos) -> dict:
    """方案一（manager.tm_no_vote_weight）：方向由投票角色共识定，交易经理不能翻转多空。
    交易经理 go 但开仓/挂单方向与共识方向相反 → 改为否决（不下单）。同时标记「交易经理否决共识」：
    共识有方向（|S|>阈值）而交易经理否决（含翻转被拦）。开关关闭时只记标记，不改状态。"""
    th = float(_mcfg(cfg).get("consensus_threshold", 0.1))
    side = "long" if S > th else ("short" if S < -th else None)
    out = {"status": status, "consensus_side": side, "vetoed_consensus": False, "flip_blocked": False, "note": None}
    if absent:
        return out
    if tm_no_vote(cfg) and status == "go" and side:
        d = tm_stance(dec, mk, pos)["direction"]
        if d in ("bullish", "bearish") and d != ("bullish" if side == "long" else "bearish"):
            out.update(status="veto", flip_blocked=True,
                       note=f"交易经理方向（{'多' if d == 'bullish' else '空'}）与共识方向（{'多' if side == 'long' else '空'}，S={S:+.3f}）相反："
                            "交易经理不能翻转多空，按否决处理（不下单）")
    out["vetoed_consensus"] = bool(side and out["status"] == "veto")
    return out


def decide_and_execute(cfg, coin, trade_date, fs, st, tracker, source="daily") -> dict:
    inst = coin["inst_id"]
    mk = market_ctx(inst)
    ap = fs.get("_analysis_px")          # 分析开始时的参考价（run_daily 串行/并行两路都记）；没有则不做漂移检查
    if ap:
        mk["_analysis_ref"] = ap
    prices = {inst: mk["last"]}
    from desk import demo_exec
    bk = demo_exec.book(cfg, prices) if demo_exec.is_demo(cfg) else ledger.book(cfg, prices)
    pos = next((p for p in bk["positions"] if p["inst_id"] == inst), None)
    w = weights(cfg)
    S = consensus(st, w)          # 交易经理提示词里展示的是不含交易经理本人的共识（方案一下即最终共识）
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
    try:
        preview = add_preview(cfg, inst, mk, bk, pos, st, w, S)
    except Exception as e:   # 预览只是信息，算不出不阻断
        preview = {"items": [], "error": f"预览计算失败（{type(e).__name__}）", "base_sl": (pos or {}).get("sl_px"), "base_tp": (pos or {}).get("tp_px")} if pos else None
    tok = dllm.STAGE.set(f"{inst}/交易经理")
    try:
        # L5：交易经理也读自己的角色手册（playbooks/trade_manager 当前版本）
        model = roles.PlaybookLLM(dllm.make_llm(cfg, "deep", tracker, role="trade_manager").with_structured_output(ManagerDecision),
                                  "trade_manager")
        dec = model.invoke(_prompt(cfg, coin, mk, pos, bk, st, w, S, texts, preview))
        if isinstance(dec, dict):
            dec = ManagerDecision(**dec) if dec else None
        absent, absent_why = False, None
        if not isinstance(dec, ManagerDecision):
            # 空输出（None/空字典/非结构化结果）：视为交易经理无结论
            absent, absent_why = True, "交易经理输出为空"
            dec = ManagerDecision(action="hold", confidence=0.0, rationale=f"{absent_why}：{TM_NO_CONCLUSION_NOTE}")
    except Exception as e:
        # 调用失败：不再用默认中性 0.5 票计入共识，这一票直接不计；保守处理，本次不开新仓（不算否决）
        n = type(e).__name__
        absent_why = (f"交易经理输出解析失败（{n}）" if any(k in n for k in ("Pars", "Validation", "JSON", "Output"))
                      else f"交易经理调用失败（{n}）")
        dec = ManagerDecision(action="hold", confidence=0.0, rationale=f"{absent_why}：{TM_ABSENT_NOTE}")
        absent = True
    finally:
        dllm.STAGE.reset(tok)

    st = dict(st)
    status = "absent" if absent else tm_status(dec, pos, mk)
    if not absent:
        st["trade_manager"] = tm_stance(dec, mk, pos)
    gate = tm_direction_gate(cfg, S, dec, status, absent, mk, pos)
    status = gate["status"]
    res = pm_execute(cfg, coin, did, st, w, mk, bk, pos, dec.rationale, source=source, tm=None if absent else dec,
                     tm_state=status)
    action, size, notes, execd, agg = res["action"], res["size_pct"], res["notes"], res["execution"], res["aggregate"]
    if gate.get("note"):
        notes.insert(0, gate["note"])
    if absent and tm_required_for_open(cfg):
        notes.insert(0, f"{TM_NO_CONCLUSION_NOTE}（{absent_why}）")
    S_pre_tm = S   # 交易经理提示里看到的口径（不含其本人投票）
    S = agg["S"]
    apply_position_sl_tp(cfg, pos, st, w, mk, dec, action, execd, notes, tm_absent=absent)

    rec = {"decision_id": did, "date": trade_date, "session": record_session(source), "time": settings.now_str(), "inst_id": inst, "ticker": coin["ticker"],
           "price": mk["last"], "market": mk, "consensus": round(S, 4), "consensus_pre_tm": round(S_pre_tm, 4), "weights": w,
           "ta_signal": fs.get("_signal"), "manager": dec.model_dump(), "final_action": action, "size_pct": size,
           "notes": notes, "execution": execd, "stances": st, "aggregate": agg, "source": source,
           "pending": res.get("pending"), "tm_status": status, "tm_absent_reason": absent_why if absent else None,
           "analysis_px": (ap or {}).get("px"), "analysis_px_time": (ap or {}).get("time"),
           "drift_check": agg.get("drift_check"), "drift_skipped": agg.get("drift_skipped"), "add_preview": preview,
           "tm_label_mismatch": None if absent else tm_label_mismatch(dec.model_dump()),
           "field_missing": None if absent else (tm_field_issues(dec.model_dump())["missing"] or None),
           "stance_field_conflicts": {r: s["field_conflict"] for r, s in st.items() if s.get("field_conflict")} or None,
           "consensus_side": gate.get("consensus_side"), "tm_vetoed_consensus": gate.get("vetoed_consensus"),
           "tm_flip_blocked": gate.get("flip_blocked") or None,
           "config_hash": settings.config_hash(cfg), "playbook_versions": roles.versions()}   # L1 追溯
    if tm_attribution(cfg):   # 方案一 B：每一单的盈亏挂到交易经理名下
        rec["decided_by"] = "trade_manager"
        rec["decision_type"] = decision_type(action, dec, pos, absent)
    settings.append_jsonl(DECISIONS, rec)
    if absent:   # 交易经理无结论：独立即时提醒（按决策号去重，同一币同一次讨论一次）
        try:
            from desk.demo_ws import notify_tm_no_conclusion
            notify_tm_no_conclusion(inst, absent_why, did, source)
        except Exception:
            pass
    return rec


def _rr_guard_ok(cfg, pos, new_stop, new_tp, notes, label: str) -> bool:
    """TM new_* 与加权调点共用：相对均价的 RR 不得低于 risk.min_rr_after_adjust。缺止盈时不拦止损收紧。"""
    from desk import sizing
    entry = float(pos.get("avg_px") or 0)
    stop = new_stop if new_stop is not None else pos.get("sl_px")
    tp = new_tp if new_tp is not None else pos.get("tp_px")
    min_rr = float(sizing.rcfg(cfg).get("min_rr_after_adjust", 1.5))
    ok, msg, _rr = sizing.guard_rr(pos["side"], entry, stop, tp, min_rr)
    if not ok:
        notes.append(f"{label} 未执行：{msg}")
    return ok


def apply_new_stop(cfg, pos, dec, action, execd, notes):
    """交易经理 new_stop：hold 与减仓成功后都执行，只许收紧（多单只上移、空单只下移），且调后 RR≥min_rr_after_adjust。
    减仓后按剩余张数挂止损单。"""
    from desk import demo_exec
    reduced = action == "reduce" and isinstance(execd, dict) and execd.get("ok")
    if pos and dec.new_stop and action == "reduce" and not reduced:
        notes.append("减仓未成功，new_stop 未执行")
    if pos and dec.new_stop and (action.startswith("hold") or reduced):
        if reduced and execd.get("remaining") is not None:   # 减仓后止损单张数须等于剩余仓位
            pos = {**pos, "contracts": execd["remaining"]}
        # 与加权调点同一护栏：能算 RR 时必须 ≥ min_rr（止盈取 new_tp 或原 tp）
        if not _rr_guard_ok(cfg, pos, float(dec.new_stop), getattr(dec, "new_tp", None), notes, "new_stop"):
            return
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
    止盈可调近或调远，但须在现价盈利一侧且不立即触发；调后 RR（相对均价）≥ min_rr_after_adjust。"""
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
    if not _rr_guard_ok(cfg, pos, getattr(dec, "new_stop", None), float(tp), notes, "new_tp"):
        return
    if pos.get("venue") != "okx_demo":
        notes.append("非模拟盘仓位，new_tp 未执行"); return
    try:
        ok, msg = demo_exec.move_tp(cfg, pos, float(tp))
    except Exception as e:
        msg = f"模拟盘改止盈失败：{type(e).__name__}: {e}"
    notes.append(msg)


HOLD_KEEP_NOTE = "交易经理持有不动，保持原止损止盈"


def hold_keep_sltp(cfg, dec, action, tm_absent=False) -> bool:
    """2026-10-28 用户决定：交易经理（实际到场）决策为 hold、且 new_stop/new_tp 都留空时，保持原止损止盈，跳过全员加权调点。
    开关 manager.tm_hold_keep_sltp（缺省 True）；交易经理缺席/调用失败不算"明说不动"，仍走原逻辑。"""
    if not bool((cfg.get("manager") or {}).get("tm_hold_keep_sltp", True)):
        return False
    if tm_absent or getattr(dec, "action", None) != "hold" or not str(action).startswith("hold"):
        return False
    return getattr(dec, "new_stop", None) is None and getattr(dec, "new_tp", None) is None


PENDING_KEEP_NOTE = "交易经理投条件单/条件加仓且未给新止损止盈：原仓保持原点位（不重新加权、不外推止盈）"


def pending_keep_position_sltp(cfg, dec, action, tm_absent=False) -> bool:
    """2026-10-30（feat/sltp-anchor-fix 规则 3）：交易经理（实际到场）投 pending（含条件加仓 pending_add、或条件单未挂出仍为 hold），
    且 new_stop/new_tp 都留空 → 原仓保持原点位。开关 manager.tm_pending_keep_sltp（缺省 True）；主开关 tm_hold_keep_sltp 关闭时一并关闭。"""
    M = cfg.get("manager") or {}
    if not bool(M.get("tm_hold_keep_sltp", True)) or not bool(M.get("tm_pending_keep_sltp", True)):
        return False
    if tm_absent or getattr(dec, "action", None) != "pending":
        return False
    if not (str(action).startswith("hold") or str(action) in ("pending", "pending_add")):
        return False
    return getattr(dec, "new_stop", None) is None and getattr(dec, "new_tp", None) is None


HOLD_SINGLE_NOTE = "交易经理持有只给单项调点，未给的一项保持原值（不套加权）"


def hold_keep_single(cfg, dec, action, tm_absent=False) -> bool:
    """2026-10-29 用户决定：交易经理（实际到场）hold 且 new_stop、new_tp 只给了一项 → 给的按其值（过原护栏），没给的保持原值。
    子开关 manager.tm_hold_keep_single_sltp（缺省 True）；主开关 tm_hold_keep_sltp 关闭时一并回到旧行为。"""
    M = cfg.get("manager") or {}
    if not bool(M.get("tm_hold_keep_sltp", True)) or not bool(M.get("tm_hold_keep_single_sltp", True)):
        return False
    if tm_absent or getattr(dec, "action", None) != "hold" or not str(action).startswith("hold"):
        return False
    return (getattr(dec, "new_stop", None) is None) != (getattr(dec, "new_tp", None) is None)


def apply_position_sl_tp(cfg, pos, st, w, mk, dec, action, execd, notes, tm_absent=False):
    """已开仓调点（早/晚场与开仓同一套）：ATR+2R 基准 → 全员 w×置信度加权 → TM new_* 最终微调；
    RR≥min_rr_after_adjust；止损只许收紧。无 ATR 时仅走 TM new_*（仍受 RR 护栏）。"""
    from desk import demo_exec, sizing
    reduced = action == "reduce" and isinstance(execd, dict) and execd.get("ok")
    if pos and action == "reduce" and not reduced:
        if getattr(dec, "new_stop", None):
            notes.append("减仓未成功，new_stop 未执行")
        if getattr(dec, "new_tp", None):
            notes.append("减仓未成功，new_tp 未执行")
        return
    if not (pos and (action.startswith("hold") or reduced or action == "pending_add")):
        return
    if reduced and execd.get("remaining") is not None:
        pos = {**pos, "contracts": execd["remaining"]}
    if pos.get("legacy"):
        if getattr(dec, "new_stop", None):
            notes.append("旧纸面仓由旧仓盯盘管理，不改止损")
        return

    if hold_keep_sltp(cfg, dec, action, tm_absent):
        notes.append(HOLD_KEEP_NOTE)
        return
    if pending_keep_position_sltp(cfg, dec, action, tm_absent):
        notes.append(PENDING_KEEP_NOTE)
        return
    single = hold_keep_single(cfg, dec, action, tm_absent)
    if single:
        notes.append(HOLD_SINGLE_NOTE)
    rc = sizing.rcfg(cfg)
    min_rr = float(rc.get("min_rr_after_adjust", 1.5))
    side = pos["side"]
    entry = float(pos.get("avg_px") or 0)
    atr = (mk or {}).get("atr14")
    proposed_stop = proposed_tp = None   # single：不算加权方案 → 未给的一项为 None=保持原值；给的一项护栏不过也回退 None=保持原值
    if atr and entry > 0 and not single:
        base_stop, base_tp, _ = sizing.stop_take(side, entry, float(atr), rc)
        stn = {r: _norm_stance(s) for r, s in (st or {}).items()}
        roles = [r for r in valid_voters(stn) if r in (w or {})]
        sc = sizing.scfg(cfg)
        if sc["sltp_exclude_self_levels"]:   # 规则 4：与现有止盈/止损同价的角色点位不回收进加权
            stn, dropped = sizing.drop_self_levels(stn, pos, float(sc["same_px_tol_pct"]))
            if dropped:
                notes.append("加权排除与现有点位同价的角色点位（防自我强化）："
                             + "、".join(f"{r}.{'止盈' if f == 'target' else '止损'}={float(v):g}" for r, f, v in dropped))
        lv = sizing.wavg_levels(stn, w or {}, roles, side, entry)
        fin = sizing.finalize_sl_tp(side, entry, base_stop, base_tp, lv.get("stop"), lv.get("target"), min_rr,
                                    **sizing.anchor_opts(cfg, rc, atr, entry))
        proposed_stop, proposed_tp = fin["stop"], fin["take_profit"]
        for n in fin["notes"]:
            notes.append(n)

    tm_stop = getattr(dec, "new_stop", None)
    tm_tp = getattr(dec, "new_tp", None)
    final_stop = float(tm_stop) if tm_stop is not None else proposed_stop
    final_tp = float(tm_tp) if tm_tp is not None else proposed_tp

    # TM 微调也必须过 RR；不过则回退到加权方案（若有），再不行则该维不改
    if tm_stop is not None or tm_tp is not None:
        chk_stop = final_stop if final_stop is not None else pos.get("sl_px")
        chk_tp = final_tp if final_tp is not None else pos.get("tp_px")
        ok, msg, rr = sizing.guard_rr(side, entry, chk_stop, chk_tp, min_rr)
        if not ok:
            notes.append(f"交易经理 new_* 未采纳：{msg}")
            final_stop = proposed_stop if tm_stop is not None else final_stop
            final_tp = proposed_tp if tm_tp is not None else final_tp
            # 若回退后仍是 TM 原值且无加权方案，则该维清空不改
            if tm_stop is not None and proposed_stop is None:
                final_stop = None
            if tm_tp is not None and proposed_tp is None:
                final_tp = None
        elif tm_stop is not None or tm_tp is not None:
            notes.append(f"交易经理最终调点" + (f" 止损→{final_stop:.6g}" if tm_stop is not None and final_stop is not None else "")
                         + (f" 止盈→{final_tp:.6g}" if tm_tp is not None and final_tp is not None else "")
                         + (f"（RR={rr:.3f}）" if rr is not None else ""))

    # 执行：止损只在收紧时提交；止盈有变化就提交
    if final_stop is not None:
        old = pos.get("sl_px")
        try:
            old_f = float(old) if old is not None else None
        except (TypeError, ValueError):
            old_f = None
        tighten = old_f is None or (final_stop > old_f if side == "long" else final_stop < old_f)
        same = old_f is not None and abs(final_stop - old_f) < 1e-9
        if same:
            pass
        elif not tighten and not (cfg.get("manager") or {}).get("allow_stop_widen"):
            notes.append(f"加权/TM 止损 {final_stop:.6g} 宽于现止损 {old_f:.6g}，不放宽")
        elif pos.get("venue") == "okx_demo":
            try:
                ok, msg = demo_exec.move_stop(cfg, pos, float(final_stop))
            except Exception as e:
                msg = f"模拟盘改止损失败：{e}"
            notes.append(msg)
        elif pos.get("trade_id"):
            ok, msg = ledger.move_stop(cfg, pos["trade_id"], float(final_stop))
            notes.append(msg)

    if final_tp is not None and pos.get("venue") == "okx_demo":
        old_tp = pos.get("tp_px")
        try:
            same_tp = old_tp is not None and abs(float(final_tp) - float(old_tp)) < 1e-9
        except (TypeError, ValueError):
            same_tp = False
        if not same_tp:
            try:
                ok, msg = demo_exec.move_tp(cfg, pos, float(final_tp))
            except Exception as e:
                msg = f"模拟盘改止盈失败：{type(e).__name__}: {e}"
            notes.append(msg)
    elif final_tp is not None and tm_tp is not None and pos.get("venue") != "okx_demo":
        notes.append("非模拟盘仓位，new_tp 未执行")


def pm_execute(cfg, coin, did, st, w, mk, bk, pos, rationale, source="daily", tm=None, tm_state=None) -> dict:
    """统一开仓流程（每日 08:00 与止损后再讨论共用）：各角色立场 → 加权聚合 → 组合经理执行。
    组合经理不投票：只把聚合结果过护栏后，现价下纸面单，或挂/替换条件入场单。
    已有持仓时的卖出/减仓仍按交易经理的决策执行（不属于开仓路径）。"""
    from desk import demo_exec, pending
    if demo_exec.is_demo(cfg):
        return pm_execute_demo(cfg, coin, did, st, w, mk, bk, pos, rationale, source, tm, tm_state)
    inst = coin["inst_id"]
    agg = _agg(cfg, st, w, mk, None, tm)
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
    elif drift.skip(cfg, inst, did, mk, agg, notes, "open"):
        action = "hold(价格漂移)"
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
                                for r in VOTERS if r in st and r in w}})
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
                "aggregate": _agg(cfg, st, w, mk, None, tm), "pending": None}


def sizing_same_px(cfg, a, b) -> bool:
    from desk import sizing
    return sizing.same_px(a, b, float(sizing.scfg(cfg)["same_px_tol_pct"]))


def _drift_exempt(cfg, inst, did, agg, notes, tm_pend) -> bool:
    """manager.tm_pending_drift_exempt（默认 true）：交易经理条件单（触发价是绝对价位）不因 1% 漂移被跳过，
    除非现价已越过触发价（会立即触发，等同现价开仓）。现价单、加仓与聚合出的非交易经理条件单仍按漂移检查跳过。"""
    if not tm_pend or not (cfg.get("manager") or {}).get("tm_pending_drift_exempt", True):
        return False
    d = agg.get("drift_skipped") or {}
    if _trigger_crossed(agg.get("condition"), float(agg["entry_px"]), float(d.get("now_px") or 0)):
        return False
    agg["drift_exempted"] = agg.pop("drift_skipped", None)
    from desk import okx_demo
    okx_demo.log_event("drift_exempt_tm_trigger", inst_id=inst, decision_id=did, **(agg["drift_exempted"] or {}))
    notes.append("交易经理条件单触发价为绝对价位、现价未越过触发价：不因价格漂移跳过，按条件单原流程挂单")
    return True


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
    """决策记录的场次：止盈/止损后再讨论记 rediscuss（不再误记为 morning），持有期到期全员讨论记 expiry，其余按当前场次。"""
    if str(source).startswith("expiry"):
        return "expiry"
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


def tm_pending_override(cfg, source, tm, tm_state, agg, notes) -> bool:
    """改动 3（manager.tm_pending_override，默认 true）：把止盈后再讨论的规则推广到早晚场与止损后再讨论。
    止盈后再讨论仍走 post_tp_override 原规则。其余来源：交易经理 go 且 action=pending、点位完整、方向与聚合方向一致、
    聚合结果允许入场（buy）时，不管聚合选的是现价还是别的价位，一律改按交易经理的触发价、条件、止损、止盈挂条件单，返回 False。
    方向相反或聚合不入场时不改（沿用原流程）。返回值语义同 post_tp_override（True=观望不下单，这里恒为 False）。"""
    if str(source).startswith("post_tp"):
        return post_tp_override(source, tm, tm_state, agg, notes)
    if not (cfg.get("manager") or {}).get("tm_pending_override", True):
        return False
    if tm is None or tm_state != "go" or tm.action != "pending" or not pending_complete(tm) or not agg.get("buy"):
        return False
    side = "long" if tm.entry_side == "long" else "short"
    if side != agg.get("side"):
        notes.append("交易经理条件单方向与共识相反：不改挂交易经理条件单，按聚合结果执行")
        return False
    before = (agg.get("condition"), agg.get("entry_px"))
    agg.update({"entry_px": float(tm.entry_trigger_px), "condition": tm.entry_condition, "stop_loss": float(tm.entry_stop_loss),
                "take_profit": float(tm.entry_take_profit) if tm.entry_take_profit else None, "plan_group": "tm_pending",
                "plan_before_tm": {"condition": before[0], "entry_px": before[1]}})
    lab = "止损后再讨论" if str(source).startswith("post_stop") else "早晚场"
    notes.append(f"{lab}：交易经理投挂条件单且点位完整 → 不追现价，改按交易经理点位挂条件单"
                 f"（{'≥' if tm.entry_condition == 'gte' else '≤'} {float(tm.entry_trigger_px):g}，止损 {float(tm.entry_stop_loss):g}，"
                 f"止盈 {tm.entry_take_profit if tm.entry_take_profit else '无'}；聚合原方案 "
                 f"{'现价' if before[0] == 'now' else before[0]} {before[1] if before[1] is None else f'{float(before[1]):g}'}）")
    return False


def keep_same_trigger(cfg, inst, mk, agg, ex, tm, notes) -> dict:
    """交易经理条件单与现有同向挂单同价位：保留原单不重挂，只按 new 止损/止盈规则改点（sizing.pending_keep_sltp）。"""
    from desk import demo_exec as DX, sizing
    rc, M = sizing.rcfg(cfg), cfg.get("manager") or {}
    r = sizing.pending_keep_sltp(ex["side"], ex["trigger_px"], ex.get("stop_loss"), ex.get("take_profit"),
                                 float(tm.entry_stop_loss) if tm.entry_stop_loss else None,
                                 float(tm.entry_take_profit) if tm.entry_take_profit else None,
                                 float(mk["last"]), float(rc.get("min_rr_after_adjust", 1.5)), float(M.get("tp_min_gap_pct", 0.3)))
    for x in r["reasons"]:
        notes.append("保留现有条件单改点：" + x)
    info = {"kept": ex["algoId"], "trigger_px": ex["trigger_px"], "side": ex["side"], "old_sl": ex.get("stop_loss"),
            "old_tp": ex.get("take_profit"), "stop_loss": r["stop_loss"], "take_profit": r["take_profit"], "rr": r["rr"],
            "reasons": r["reasons"]}
    if r["changed"]:
        res = DX.amend_trigger_sltp(cfg, inst, ex, r["stop_loss"] if r["stop_loss"] != ex.get("stop_loss") else None,
                                    r["take_profit"] if r["take_profit"] != ex.get("take_profit") else None)
        info.update(amend=res, algoId=res.get("algoId"))
        notes.append(f"保留现有条件单 {ex['algoId']}（{ex['trigger_px']:g}），止损 {ex.get('stop_loss')}→{r['stop_loss']}，"
                     f"止盈 {ex.get('take_profit')}→{r['take_profit']}（{ {'amend': '原单修改', 'replace': '重挂替换', 'failed': '修改失败，保持原值'}.get(res['how'], res['how']) }）")
    else:
        info["algoId"] = ex["algoId"]
        notes.append(f"保留现有条件单 {ex['algoId']}（{ex['trigger_px']:g}），止损止盈不变")
    agg.update(stop_loss=info["stop_loss"], take_profit=info["take_profit"], plan_group="tm_pending_keep")
    return info


def _trigger_crossed(cond, trig, px) -> bool:
    return (px >= trig) if cond == "gte" else (px <= trig) if cond == "lte" else True


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


def add_preview(cfg, inst, mk, bk, pos, st, w, S=None) -> dict | None:
    """交易经理决策前的「加仓预览」（只读信息，不下单、不改点位）。
    已有持仓且 allow_add_position 开启时，对各角色提出的同向入场价与现价，按加仓入场价独立算本腿止损/止盈/RR
    （add_position.plan_sl_tp），并用 add_position.budget 预算加仓张数与「原仓风险+本腿风险」是否在单笔上限内。
    任一步算不出来只写原因，不抛错。无持仓或开关关闭返回 None。"""
    if not pos or pos.get("legacy") or not add_position.enabled(cfg):
        return None
    from desk import sizing, okx_demo
    side = pos["side"]
    want = "bullish" if side == "long" else "bearish"
    out = {"side": side, "base_sl": pos.get("sl_px"), "base_tp": pos.get("tp_px"), "items": [], "error": None}
    try:
        rc = sizing.rcfg(cfg)
        last = float(mk["last"])
        cands = [("现价", last)]
        for r in ROLE_KEYS:
            s = _norm_stance((st or {}).get(r) or {})
            px = s.get("entry_px")
            if s.get("direction") != want or px in (None, ""):
                continue
            try:
                px = float(px)
            except (TypeError, ValueError):
                continue
            if px > 0:
                cands.append((ROLE_CN.get(r, r), px))
        merged: dict = {}
        for who, px in cands:
            k = round(px, 8)
            merged.setdefault(k, {"px": px, "by": []})["by"].append(who)
        vv = valid_voters(st or {})
        try:
            legs = (DXreg().get("positions") or {}).get(inst, {}).get("legs")
        except Exception:
            legs = None
        S0 = float(S if S is not None else consensus(st or {}, w or {}))
        S_use = (1 if side == "long" else -1) * max(abs(S0), 1e-6)
        for k, m in merged.items():
            px = m["px"]
            it = {"by": m["by"], "add_px": px, "stop": None, "tp": None, "rr": None, "sz": None,
                  "existing_risk": None, "add_risk": None, "cap": None, "within_budget": None, "reason": None, "notes": []}
            out["items"].append(it)
            try:
                lv = sizing.wavg_levels(st or {}, w or {}, vv, side, px) if vv else {}
                plan = add_position.plan_sl_tp(rc, side, px, mk.get("atr14"), lv.get("stop"), lv.get("target"))
                it["notes"] = list(plan.get("notes") or [])
                if plan["stop"] is None:
                    it["reason"] = "加仓腿止损无效"; continue
                it.update(stop=plan["stop"], tp=plan["take_profit"], rr=plan["rr"])
                if "live_equity" not in bk:
                    it["reason"] = "非模拟盘账户，无法按风险定仓预算张数"; continue
                sz_info = demo_size(cfg, {"S": S_use, "side": side, "entry_px": px, "stop_loss": float(plan["stop"])}, mk, bk, None, inst)
                if not sz_info.get("ok"):
                    it["reason"] = f"定仓拒绝：{sz_info.get('reason')}"; continue
                a = add_position.budget(rc, pos, px, float(plan["stop"]), sz_info,
                                        float(bk["live_equity"] if bk.get("live_equity") else bk["equity"]),
                                        float(cfg["guardrails"]["max_position_pct"]),
                                        float(cfg["guardrails"].get("min_order_usd", 0)), legs=legs)
                it.update(existing_risk=a.get("existing_risk"), add_risk=a.get("add_risk"), cap=a.get("cap"),
                          clipped=a.get("clipped"), clip_by=a.get("clip_by"))
                if not a["ok"]:
                    it.update(within_budget=False, reason=a.get("reason")); continue
                sz = okx_demo.contracts(inst, a["notional"] / px)
                if not sz:
                    it.update(within_budget=False, reason="size_too_small"); continue
                it.update(sz=sz, within_budget=(float(a["existing_risk"]) + float(a["add_risk"]) <= float(a["cap"]) + 1e-6))
            except Exception as e:
                it["reason"] = f"预览计算失败（{type(e).__name__}: {str(e)[:80]}）"
    except Exception as e:
        out["error"] = f"预览计算失败（{type(e).__name__}: {str(e)[:120]}）"
    return out


def DXreg() -> dict:
    from desk import demo_exec as DX
    return DX.reg()


def _fmt(v) -> str:
    return "—" if v is None else (format(float(v), ".6g") if isinstance(v, (int, float)) else str(v))


def describe_add_preview(pv: dict | None) -> str:
    """加仓预览文本（交易经理提示词与日报共用）。"""
    if not pv:
        return ""
    head = (f"加仓预览（仅供决策参考，不下单不改点位；加仓腿按加仓入场价独立算止损止盈，只覆盖加仓张数；"
            f"原仓止损 {_fmt(pv.get('base_sl'))}、止盈 {_fmt(pv.get('base_tp'))} 不动）")
    if pv.get("error"):
        return head + "：" + pv["error"]
    rows = []
    for it in pv.get("items") or []:
        s = (f"{'/'.join(it['by'])} 入场 {_fmt(it['add_px'])}：本腿止损 {_fmt(it['stop'])}、止盈 {_fmt(it['tp'])}、"
             f"RR {('%.2f' % it['rr']) if it.get('rr') is not None else '—'}、加仓 {it['sz'] or '—'} 张")
        if it.get("cap") is not None:
            s += (f"、原仓风险 {float(it['existing_risk'] or 0):.2f}+本腿风险 {float(it['add_risk'] or 0):.2f}"
                  f" / 上限 {float(it['cap']):.2f} USDT（0.75%预算"
                  + ("内" if it.get("within_budget") else "不足") + (f"，已削减：{'、'.join(it.get('clip_by') or [])}" if it.get("clipped") else "") + "）")
        if it.get("reason"):
            s += f"；算不出/不可加：{it['reason']}"
        rows.append(s)
    return head + "：" + ("；".join(rows) if rows else "无可预览的同向入场价")


def _try_add(cfg, inst, did, agg, mk, bk, pos, st, w, tm, tm_state, source, rationale, notes, tp_hold):
    """同向加仓：共识同向 → 交易经理可否决 → 按加仓入场价独立算止盈止损（ATR+2R+全员加权，RR≥1.5）→
    与新开仓同一 sizing.size（止损=本腿止损）→ 原仓风险+本腿风险≤单笔上限（不足削减）→ 现价/条件加仓。
    本腿独立挂 reduceOnly 保护，原仓止盈止损不动。返回 (action, execd, pend, size_pct)。"""
    from desk import demo_exec as DX, okx_demo, sizing
    side = pos["side"]
    agg["add"] = None
    if tm_state in ("veto", "absent"):
        why = ((getattr(tm, "rationale", "") or "").strip()[:120] if tm is not None else "") or "交易经理缺席"
        notes.append(f"同向加仓：交易经理{'否决' if tm_state == 'veto' else '缺席'}，不加仓（原因：{why}；共识 S={agg['S']:+.3f}）")
        okx_demo.log_event("add_vetoed", inst_id=inst, decision_id=did, tm_state=tm_state, reason=why)
        return ("hold(交易经理否决)" if tm_state == "veto" else "hold(交易经理缺席)"), None, None, 0.0
    if tp_hold:
        return "hold(止盈后不追单)", None, None, 0.0
    if drift.skip(cfg, inst, did, mk, agg, notes, "add"):
        return "hold(价格漂移)", None, None, 0.0
    if _cooldown(cfg, inst, agg, did, notes):
        return "hold(止损冷却)", None, None, 0.0
    gate = pre_trade_gate(cfg, inst, mk)
    if gate:
        notes.append(f"执行前复核未通过（{gate}），本次不加仓")
        return "hold", None, None, 0.0
    cur_sl, cur_tp = pos.get("sl_px"), pos.get("tp_px")
    if cur_sl in (None, ""):
        notes.append("现有持仓没有止损，不加仓")
        return "hold", None, None, 0.0
    now = agg["condition"] == "now"
    add_px = float(mk["last"]) if now else float(agg["entry_px"])
    rc = sizing.rcfg(cfg)
    lv = sizing.wavg_levels(st, w, valid_voters(st), side, add_px)
    plan = add_position.plan_sl_tp(rc, side, add_px, mk.get("atr14"), lv.get("stop"), lv.get("target"))
    if plan["stop"] is None:
        notes.append("加仓腿止损无效，不加仓"); notes += plan["notes"]
        return "hold", None, None, 0.0
    notes += plan["notes"]
    sz_info = demo_size(cfg, {**agg, "entry_px": add_px, "stop_loss": float(plan["stop"])}, mk, bk, tm, inst)
    if not sz_info.get("ok"):
        notes.append(f"同向加仓定仓拒绝：{sz_info.get('reason')}")
        okx_demo.log_event("add_rejected", inst_id=inst, decision_id=did, reasons=[sz_info.get("reason")])
        return "hold", None, None, 0.0
    r = DX.reg(); legs = (r.get("positions") or {}).get(inst, {}).get("legs")
    a = add_position.budget(rc, pos, add_px, float(plan["stop"]), sz_info,
                            float(bk["live_equity"] if bk.get("live_equity") else bk["equity"]),
                            float(cfg["guardrails"]["max_position_pct"]), float(cfg["guardrails"].get("min_order_usd", 0)),
                            legs=legs)
    a.update(add_px=add_px, mode=("now" if now else "trigger"), base_sl=cur_sl, base_tp=cur_tp,
             final_sl=plan["stop"], final_tp=plan["take_profit"], rr=plan["rr"])
    agg["add"] = a
    if not a["ok"]:
        notes.append(add_position.describe(a))
        okx_demo.log_event(a["reason"], inst_id=inst, decision_id=did, side=side, **{k: a.get(k) for k in ("existing_risk", "cap", "budget_left")})
        if a["reason"] == "add_budget_full":
            notes.append(f"交易经理/组合经理：同向加仓预算已满（现有风险 {a['existing_risk']:.2f} ≥ 上限 {a['cap']:.2f}），不加仓")
        return "hold", None, None, 0.0
    ct = float(okx_demo.instrument(inst)["ctVal"])
    sz = okx_demo.contracts(inst, a["notional"] / add_px)
    if not sz:
        a.update(ok=False, reason="size_too_small"); notes.append(add_position.describe(a))
        return "hold", None, None, 0.0
    a["sz"] = sz
    qty_add = float(sz) * ct
    notes.append(add_position.describe(a))
    if now:
        ok, info = DX.add_now(cfg, bk, inst, pos, sz, plan["stop"], plan["take_profit"], did, add_px, a)
        execd = {"type": "add", "ok": ok, **info}
        if not ok:
            notes.append("加仓护栏拒单：" + "；".join(info["reasons"]))
            return "hold(护栏拒单)", execd, None, 0.0
        notes.append(f"同向加仓 {sz} 张 @≈{add_px:.6g}（独立腿），本腿止损 {plan['stop']:.6g}、止盈 "
                     f"{format(plan['take_profit'], '.6g') if plan['take_profit'] is not None else '无'}，RR {plan['rr']}；"
                     f"原仓止损 {cur_sl}、止盈 {cur_tp} 不动")
        return ("add_long" if side == "long" else "add_short"), execd, None, round(qty_add * add_px / float(pos.get("leverage") or 1) / bk["equity"] * 100, 4)
    exp = (tm.entry_expire_hours if tm is not None and getattr(tm, "entry_expire_hours", None) else None) \
        or cfg.get("pending", {}).get("default_expire_hours", 24)
    meta = {"base_contracts": pos["contracts"], "base_avg": pos["avg_px"], "base_sl": float(cur_sl), "base_tp": cur_tp,
            "base_qty": pos["qty"], "add_px": add_px, "risk_cap_usd": a["cap"],
            "final_sl": plan["stop"], "final_tp": plan["take_profit"], "existing_risk": a["existing_risk"], "add_risk": a["add_risk"],
            "algoId": None}
    ok, info = DX.place_add_trigger(cfg, bk, inst, pos, {
        "decision_id": did, "source": source, "side": side, "trigger_px": add_px, "condition": agg["condition"],
        "stop_loss": plan["stop"], "take_profit": plan["take_profit"], "size_pct": 0, "horizon_days": agg["horizon_days"],
        "expire_hours": float(exp), "consensus": agg["S"],
        "reason": ("同向加仓：" + ((tm.entry_reason if tm is not None else "") or rationale or ""))[:200]}, sz, meta)
    if not ok:
        notes.append("条件加仓单护栏拒单：" + "；".join(info["reasons"]))
        return "hold(护栏拒单)", None, None, 0.0
    # 把 algoId 写回 add meta，便于成交复核识别本腿
    r = DX.reg()
    if info.get("algoId") and info["algoId"] in r.get("triggers", {}):
        ad = (r["triggers"][info["algoId"]].get("add") or {})
        ad["algoId"] = info["algoId"]
        r["triggers"][info["algoId"]]["add"] = ad
        DX.save_reg(r)
    notes.append(f"条件加仓单 {info['algoId']}：{'多' if side == 'long' else '空'} {'≥' if agg['condition'] == 'gte' else '≤'} "
                 f"{add_px:.6g}，{sz} 张；本腿止损 {plan['stop']:.6g}、止盈 {plan['take_profit']}；原仓止损止盈不动；成交时复核预算")
    return "pending_add", None, info, 0.0


def _pm_execute_demo_locked(cfg, coin, did, st, w, mk, bk, pos, rationale, source="daily", tm=None, tm_state=None) -> dict:
    """OKX 模拟盘直连版组合经理执行（禁止实盘，见 desk/demo_exec.py）：聚合 → 护栏（模拟盘数据）→ 现价市价单
    或 OKX trigger 条件单（均附带止损止盈）。不写本地纸面账本 / 本地 pending。任何模拟盘错误只记入 notes 与 demo_events。"""
    from desk import demo_exec as DX
    inst = coin["inst_id"]
    try:
        ex = None if pos else DX.existing_trigger(bk, inst)
    except Exception:
        ex = None
    agg = _agg(cfg, st, w, mk, ex, tm)
    notes, execd, pend = list(agg["notes"]), None, None
    action, size = "hold", 0.0
    cancel_ok = DX.ecfg(cfg)["cancel_pending_on_new_decision"]
    tp_hold = tm_pending_override(cfg, source, tm, tm_state, agg, notes)
    tm_pend = agg.get("plan_group") in ("tm_pending", "post_tp_tm_pending")
    same_ex = bool(tm_pend and ex and ex.get("side") == agg.get("side") and ex.get("trigger_px")
                   and sizing_same_px(cfg, ex["trigger_px"], agg.get("entry_px")))
    try:
        if pos and pos.get("legacy"):
            notes.append("该币是旧纸面仓（由旧仓盯盘管理止损/止盈/强平），本流程不操作、不加仓")
        elif pos:
            if tm is not None and tm.action == "sell":
                execd = DX.close(cfg, pos, "经理决策清仓"); action = "sell"
            elif tm is not None and tm.action == "reduce":
                f = _reduce_fraction(tm, notes)
                execd = DX.reduce(cfg, pos, f, f"经理决策减仓{f:.0%}"); action = "reduce"
            elif agg["buy"] and agg.get("side") != pos["side"]:
                notes.append("共识方向与现有持仓相反：不反手（须先平仓）→ 持有")
            elif agg["buy"] and not add_position.enabled(cfg):
                notes.append("该币已有模拟盘持仓，不加仓/不反手 → 持有（risk.allow_add_position=false）")
            elif agg["buy"]:
                action, execd, pend, size = _try_add(cfg, inst, did, agg, mk, bk, pos, st, w, tm, tm_state, source, rationale,
                                                     notes, tp_hold)
            if cancel_ok and action != "pending_add":
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
        elif same_ex:
            # 改动 3：交易经理条件单与现有同向挂单同价位 → 保持原单（不重挂、不过漂移跳过），按 new 止损/止盈规则改点
            pend = keep_same_trigger(cfg, inst, mk, agg, ex, tm, notes); action = "pending"
        elif drift.skip(cfg, inst, did, mk, agg, notes, "open") and not _drift_exempt(cfg, inst, did, agg, notes, tm_pend):
            action = "hold(价格漂移)"   # 只跳过本次新开仓/挂条件单；旧条件单、持仓不动
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
