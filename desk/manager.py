"""交易经理：LLM（深度模型）给出结构化决策，代码层做角色加权共识、仓位规则与硬护栏，然后执行（execution.venue=okx_demo 时直连 OKX 模拟盘，禁止实盘；paper 时纸面执行）。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, PrivateAttr

from desk import add_position, drift, ledger, okx_data, roles, settings, tm_order_tool
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
    # 由交易经理下单工具（tm_order_tool.to_decision）映射而来时的工具参数（不进 model_dump）
    _order_tool: dict | None = PrivateAttr(default=None)


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
            "reason": (dec.rationale or "")[:80], "source": "交易经理结构化"}


def pending_complete(dec) -> bool:
    return bool(dec.entry_side and dec.entry_trigger_px and dec.entry_condition and dec.entry_stop_loss
                and dec.entry_size_pct is not None and float(dec.entry_size_pct) > 0)


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


TM_VETO_NOTE = "交易经理否决：不下现价单、不挂条件单"
TM_ABSENT_NOTE = "交易经理缺席，不开新仓"
TM_NO_CONCLUSION_NOTE = "交易经理无结论，不开仓"


def aggregate(cfg, st: dict, w: dict, mk: dict) -> dict:
    """按角色权重×置信度加权汇总（交易经理不投票）：共识 S、方向、有效票、参考止损止盈（ATR+2R 基准 + 同向角色加权，RR≥min_rr）。
    只作方向判定与给交易经理/日报的参考；实际下单点位、张数以交易经理下单工具参数为准（_pm_execute_tool 覆盖）。"""
    from desk import sizing
    M = cfg["manager"]
    st = {r: _norm_stance(s) for r, s in st.items()}   # B1：中性/平仓立场的价位字段不参与聚合
    S = consensus(st, w)
    last = mk["last"]
    th = float(M.get("consensus_threshold", 0.1))
    side = "long" if S > th else ("short" if S < -th else None)
    want = "bullish" if side != "short" else "bearish"
    nv, need = len(valid_voters(st, w)), int(M.get("min_valid_voters", 0) or 0)
    if need and nv < need:   # L3：有效票不足 → 不交易
        side = None
    bulls = [r for r in valid_voters(st, w) if st[r]["direction"] == want and (st[r].get("confidence") or 0) > 0]

    def wavg(vals):
        vals = [(w[r] * float(st[r]["confidence"]), v) for r, v in vals if v]
        den = sum(a for a, _ in vals)
        return sum(a * v for a, v in vals) / den if den else None
    rc = sizing.rcfg(cfg)
    notes = []
    atr = mk.get("atr14")
    min_rr = float(rc.get("min_rr_after_adjust", 1.5))
    levels_meta = {"rr": None, "adjusted": False, "baseline_stop": None, "baseline_tp": None}
    stop = tp = None
    if atr:   # 参考点位：只用 ATR+2R 基准（不再按角色加权调点；实际点位以交易经理下单工具为准）
        base_stop, base_tp, _d = sizing.stop_take(side or "long", last, float(atr), rc)
        levels_meta["baseline_stop"], levels_meta["baseline_tp"] = base_stop, base_tp
        fin = sizing.finalize_sl_tp(side or "long", last, base_stop, base_tp, None, None, min_rr,
                                    **sizing.anchor_opts(cfg, rc, atr, last))
        stop, tp = fin["stop"], fin["take_profit"]
        levels_meta.update(rr=fin["rr"], adjusted=fin["adjusted"], rr_short=fin.get("rr_short"))
    hz = wavg([(r, float(st[r].get("horizon_days") or 0)) for r in bulls])
    cap = min(M["max_size_pct"], cfg["guardrails"]["max_position_pct"])
    size = cap * max(0.0, min(1.0, abs(S) / float(M.get("full_size_consensus", 0.6))))
    if side and abs(S) < M["half_size_below"]:
        size /= 2
    buy = side is not None and bool(bulls)
    if need and nv < need:
        notes.append(f"有效票不足（{nv}<{need}），不交易")
    elif not buy:
        notes.append(f"加权共识 S={S:+.3f} 在 ±{th} 之间或无同向角色，不交易")
    return {"S": round(S, 4), "buy": buy, "side": side, "entry_px": last, "condition": "now", "stop_loss": stop, "take_profit": tp,
            "size_pct": round(size, 3), "horizon_days": int(min(max(round(hz or 5), 1), 30)), "notes": notes,
            "contributors": {r: st[r].get("entry_px") for r in bulls}, "valid_voters": nv, "plan_group": None,
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
    """交易经理提示词：行情/账户/观点与硬护栏；下单参数说明由 tm_order_tool.instructions 给出（调用方拼在后面）。"""
    g = cfg["guardrails"]
    _stop_cap = min(float(g['max_stop_loss_pct']), float((cfg.get('risk') or {}).get('stop_max_pct', 0.08)) * 100)
    lines = [f"你是加密货币交易台的交易经理，负责对 {coin['name']}（OKX 永续 {coin['inst_id']}）做今日最终执行决策。",
             f"标的是 OKX USDT 永续合约（OKX 模拟盘 Demo Trading 执行，逐仓），可以做多也可以做空，新开仓杠杆上限 {__import__('desk.config_safety', fromlist=['x']).max_open_leverage(cfg):g} 倍（代码强制）。",
             f"硬性规则（代码会强制，违反会被拒单）：单笔保证金≤权益 {g['max_position_pct']}%，总名义≤权益 {g.get('max_total_notional_pct') or g['max_total_exposure_pct'] * ledger.swap_cfg(cfg)['leverage_max']:g}%，"
             f"开仓必须带止损（做多在下方、做空在上方）且止损距离≤{_stop_cap:g}%，止损必须先于强平价触发，止损只能收紧，" + ("每币一仓；同向加仓允许（加仓视为新订单，加仓腿独立止盈止损，原仓不动；" + ("是否加仓由你权衡决定：加仓风险预算只作参考数据不否决，加仓只受硬护栏约束（合并后单笔保证金、总名义、总在途风险、止损、当日亏损、紧急停止）" if add_position.tm_decides(cfg) else "已有风险+加仓风险≤单笔上限，不足按剩余削减") + "；反向不反手须先平仓）。" if add_position.enabled(cfg) else "每币一仓不加仓。"),
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
                  "加仓是新订单，该腿按加仓入场价独立设止损止盈、只覆盖加仓张数，原仓点位不动；请看上面加仓预览里该腿自己的止损距离、RR、预算参考与硬护栏可加张数。"
                  + "最终是否加仓仍由你决定（不加仓用 action=hold）。"]
    from desk import analysis as _an
    _pb = _an.pending_brief(bk)
    lines += ["", _pb.replace(_an.PENDING_NOTE, "（以下是已挂出、尚未成交的条件入场单；如何处理见文末下单工具说明）")]
    lines += ["", f"## 各角色观点（权重为历史表现给出；加权共识 S={S:+.3f}，范围 -1~+1）",
                  f"你的角色：你不投票，权重不计。S={S:+.3f} 是全部投票角色按权重×置信度汇总的加权共识，方向由它决定，你不能翻转多空。"
                  "你是最终定点位和张数的人：综合下面所有角色意见和行情账户数据，用文末的下单工具给出这一单（或 hold/cancel_pending 否决）。"
                  "各角色给的入场价只作参考，以你的工具参数为准；参数不完整或违反硬护栏会被退回让你改，代码不会替你补点位。这一单的盈亏记在你名下。"]
    for r in ROLE_KEYS:
        s = st[r]
        wl = "只执行不投票" if r not in w else f"权重 {w[r]:.2f}"
        lines.append(f"- {ROLE_CN[r]}（{wl}）：{s['direction']} 置信 {s.get('confidence')} 目标 {s.get('target')} "
                     f"止损 {s.get('stop')} 周期 {s.get('horizon_days')}天 —— {s.get('reason', '')[:80]}")
    lines += ["", "## 投资组合经理最终意见（TradingAgents）", texts["portfolio_manager"][-3000:],
              "", "## 交易员方案", texts["trader"][-2000:]]
    R = cfg.get("risk") or {}
    lines += ["", "## 硬护栏（代码强制，违反会被退回）",
              f"止损距入场≤{_stop_cap:g}%（做多在下方、做空在上方）；盈亏比 RR=止盈距/止损距≥{float(R.get('min_rr_after_adjust', 1.5)):g}；"
              "已有持仓止损只能收紧；new_tp 须在现价盈利一侧且距现价至少 "
              f"{float((cfg.get('manager') or dict()).get('tp_min_gap_pct', 0.3)):g}%；组合层保证金、总名义、在途风险、当日亏损上限见上方账户段。",
              "点位必须是具体数字，不得写区间或「沿用/附近」。没有足够把握就 hold。止损请依据结构位/ATR。理由用中文。"]
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


class TMToolFailed(Exception):
    """下单工具 3 次均未通过校验：按「交易经理无结论」处理（不开仓、发无结论提醒）。"""


def tool_context(cfg, inst, mk, bk, pos, S) -> dict:
    from desk import demo_exec as DX
    th = float(_mcfg(cfg).get("consensus_threshold", 0.1))
    try:
        ex = DX.existing_trigger(bk, inst) if DX.is_demo(cfg) else None
    except Exception:
        ex = None
    return {"inst": inst, "mk": mk, "bk": bk, "pos": pos, "existing": ex, "S": S,
            "consensus_side": "long" if S > th else ("short" if S < -th else None)}


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
        tool_res = None
        # 交易经理一律通过受限下单工具下单（严格 schema + 代码校验，拒绝原因返回重试，最多 3 次；3 次不过 = 无结论）
        model = roles.PlaybookLLM(dllm.make_llm(cfg, "deep", tracker, role="trade_manager")
                                  .with_structured_output(tm_order_tool.OrderToolCall), "trade_manager")
        tctx = tool_context(cfg, inst, mk, bk, pos, S)
        tool_res = tm_order_tool.run(cfg, model, _prompt(cfg, coin, mk, pos, bk, st, w, S, texts, preview)
                                     + tm_order_tool.instructions(tctx), tctx)
        dec = tool_res["dec"]
        if dec is None:
            raise TMToolFailed(tool_res["absent_why"])
        absent, absent_why = False, None
    except Exception as e:
        # 调用失败：不再用默认中性 0.5 票计入共识，这一票直接不计；保守处理，本次不开新仓（不算否决）
        n = type(e).__name__
        absent_why = str(e) if isinstance(e, TMToolFailed) else (f"交易经理输出解析失败（{n}）" if any(k in n for k in ("Pars", "Validation", "JSON", "Output"))
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
    if absent:
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
           "stance_field_conflicts": {r: s["field_conflict"] for r, s in st.items() if s.get("field_conflict")} or None,
           "consensus_side": gate.get("consensus_side"), "tm_vetoed_consensus": gate.get("vetoed_consensus"),
           "tm_flip_blocked": gate.get("flip_blocked") or None,
           "config_hash": settings.config_hash(cfg), "playbook_versions": roles.versions(),   # L1 追溯
           **({"tm_order_tool": {"attempts": tool_res["attempts"], "call": tool_res["call"], "rejected": tool_res["history"]}}
              if tool_res else {})}
    if tm_attribution(cfg):   # 方案一 B：每一单的盈亏挂到交易经理名下
        rec["decided_by"] = "trade_manager"
        rec["decision_type"] = decision_type(action, dec, pos, absent)
    settings.append_jsonl(DECISIONS, rec)
    if absent:   # 交易经理无结论（调用失败或工具 3 次未通过）：独立即时提醒（按决策号去重，同一币同一次讨论一次）
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


def apply_position_sl_tp(cfg, pos, st, w, mk, dec, action, execd, notes, tm_absent=False):
    """已开仓调点：只执行交易经理给的 new_stop/new_tp（过 RR≥min_rr_after_adjust 与止损只收紧护栏），留空=保持原值；
    不再做全员加权调点。交易经理缺席（无结论）时不调点。"""
    reduced = action == "reduce" and isinstance(execd, dict) and execd.get("ok")
    if pos and action == "reduce" and not reduced:
        if getattr(dec, "new_stop", None):
            notes.append("减仓未成功，new_stop 未执行")
        if getattr(dec, "new_tp", None):
            notes.append("减仓未成功，new_tp 未执行")
        return
    if tm_absent or not (pos and (action.startswith("hold") or reduced or action == "pending_add")):
        return
    if reduced and execd.get("remaining") is not None:
        pos = {**pos, "contracts": execd["remaining"]}
    if pos.get("legacy"):
        if getattr(dec, "new_stop", None):
            notes.append("旧纸面仓由旧仓盯盘管理，不改止损")
        return
    apply_new_stop(cfg, pos, dec, action, execd, notes)
    apply_new_tp(cfg, pos, dec, action, execd, notes)


def pm_execute(cfg, coin, did, st, w, mk, bk, pos, rationale, source="daily", tm=None, tm_state=None) -> dict:
    """统一开仓流程（每日 08:00 与止损后再讨论共用）：各角色立场 → 加权聚合 → 组合经理执行。
    组合经理不投票：只把聚合结果过护栏后，现价下纸面单，或挂/替换条件入场单。
    已有持仓时的卖出/减仓仍按交易经理的决策执行（不属于开仓路径）。"""
    from desk import demo_exec, pending
    if demo_exec.is_demo(cfg):
        return pm_execute_demo(cfg, coin, did, st, w, mk, bk, pos, rationale, source, tm, tm_state)
    inst = coin["inst_id"]
    agg = aggregate(cfg, st, w, mk)
    tool_levels(agg, tm, mk, None)
    notes, execd, pend = list(agg["notes"]), None, None
    action, size = "hold", 0.0
    if pos:
        if tm is not None and tm.action == "sell":
            r = ledger.close_position(cfg, pos["trade_id"], "经理决策清仓")
            execd = {"type": "close", "ok": True, "trade_id": r["trade_id"], "px": float(r["exit_px"]), "pnl": float(r["pnl_usd"])}
            action = "sell"
        elif tm is not None and tm.action == "reduce":
            f = float(tm.reduce_fraction)
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
                "reason": ((tm.rationale if tm is not None else "") or rationale or "")[:200],
                "role_levels": {r: {"entry_px": st[r].get("entry_px"), "condition": st[r].get("entry_condition"),
                                    "stop": st[r].get("stop"), "weight": w[r], "confidence": st[r].get("confidence")}
                                for r in VOTERS if r in st and r in w}})
            action = "pending"
            notes.append(f"挂{'多' if side == 'long' else '空'}单 {lev:g}x：{'≥' if agg['condition'] == 'gte' else '≤'} {agg['entry_px']:.6g}，止损 {sl:.6g}，"
                         f"仓位 {size:.1f}%，{float(exp):.0f} 小时有效")
    return {"action": action, "size_pct": size, "notes": notes, "execution": execd, "aggregate": agg, "pending": pend}


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
    """决策记录的场次：止盈/止损后再讨论记 rediscuss（不再误记为 morning），持有期到期全员讨论记 expiry，其余按当前场次。"""
    if str(source).startswith("expiry"):
        return "expiry"
    return "rediscuss" if str(source).startswith(("post_tp", "post_stop")) else settings.SESSION


def pending_widen_guard(cfg, inst, ex, bk):
    """保留挂单时止损以交易经理为准的硬护栏：返回 check(sl) -> 原因或 None。
    按原单张数：单笔风险 = 张数×面值×|触发价−止损| ≤ 权益×risk_per_trade_pct；保证金 = 名义/杠杆 ≤ 权益×max_margin_pct。
    缺权益/面值/张数 → 无法核验，拒绝放宽（保守）。"""
    from desk import sizing
    rc = sizing.rcfg(cfg)

    def check(sl):
        try:
            from desk import okx_demo as X
            eq = float((bk or {}).get("equity") or 0)
            sz = float(ex.get("sz") or 0)
            ct = float(X.instrument(inst)["ctVal"])
            trig = float(ex["trigger_px"])
        except Exception as e:
            return f"无法核验单笔风险（{type(e).__name__}）"
        if eq <= 0 or sz <= 0 or ct <= 0:
            return "缺权益/张数/面值，无法核验单笔风险"
        risk = sz * ct * abs(trig - float(sl))
        cap = eq * float(rc["risk_per_trade_pct"])
        if risk > cap * (1 + 1e-9):
            return f"单笔风险 {risk:.2f} USDT 超过上限 {cap:.2f}（{float(rc['risk_per_trade_pct']):.2%}×权益）"
        lev = float(ex.get("leverage") or 1) or 1
        mg = sz * ct * trig / lev
        mcap = eq * float(rc["max_margin_pct"])
        if mg > mcap * (1 + 1e-9):
            return f"保证金 {mg:.2f} USDT 超过上限 {mcap:.2f}（{float(rc['max_margin_pct']):.0%}×权益）"
        return None
    return check


def keep_same_trigger(cfg, inst, mk, agg, ex, tm, notes, bk=None) -> dict:
    """交易经理条件单与现有同向挂单同价位：保留原单不重挂，只按 new 止损/止盈规则改点（sizing.pending_keep_sltp）。
    挂单止损以交易经理为准（可放宽），只过硬护栏（pending_widen_guard）。"""
    from desk import demo_exec as DX, sizing
    rc, M = sizing.rcfg(cfg), cfg.get("manager") or {}
    r = sizing.pending_keep_sltp(ex["side"], ex["trigger_px"], ex.get("stop_loss"), ex.get("take_profit"),
                                 float(tm.entry_stop_loss) if tm.entry_stop_loss else None,
                                 float(tm.entry_take_profit) if tm.entry_take_profit else None,
                                 float(mk["last"]), float(rc.get("min_rr_after_adjust", 1.5)), float(M.get("tp_min_gap_pct", 0.3)),
                                 allow_widen=True, stop_max_pct=float(rc["stop_max_pct"]),
                                 widen_check=pending_widen_guard(cfg, inst, ex, bk))
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


def apply_tm_expiry(cfg, inst, algo_id, tm, notes) -> dict | None:
    """保留现有挂单时，有效期按交易经理 entry_expire_hours，自原挂单时刻起算（只改登记表 expires，到期由 expiry 模块撤单）。
    算出的到期已过 → 不改并写明。返回 {old, new} 或 None。"""
    import datetime as _d
    from desk import demo_exec as DX
    h = getattr(tm, "entry_expire_hours", None) if tm is not None else None
    if not h:
        return None
    m = (DX.reg().get("triggers") or {}).get(str(algo_id)) or {}
    created, old = m.get("created"), m.get("expires")
    if not created:
        notes.append(f"条件单 {algo_id} 登记表缺挂单时刻，无法按交易经理有效期 {float(h):g} 小时重算，到期保持 {old}")
        return None
    fmt = "%Y-%m-%d %H:%M:%S%z"
    new_dt = _d.datetime.strptime(created, fmt) + _d.timedelta(hours=float(h))
    new = new_dt.strftime(fmt)
    if new_dt <= settings.now():
        notes.append(f"交易经理有效期 {float(h):g} 小时自挂单时刻 {created} 起算已过（{new}），到期保持 {old}，请人工确认")
        return None
    if old and abs((_d.datetime.strptime(old, fmt) - new_dt).total_seconds()) < 60:
        return None
    DX.set_trigger_expiry(inst, str(algo_id), new, f"交易经理有效期 {float(h):g} 小时")
    notes.append(f"条件单 {algo_id} 有效期按交易经理 {float(h):g} 小时自挂单时刻起算：到期 {old} → {new}")
    return {"old": old, "new": new}


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


def _add_hard_kw(cfg, sz_info, eq) -> dict:
    """加仓硬护栏参数：总名义上限与剩余额度（持仓+其他币条件单+在途子单，同 demo_size 的 book_exposure）。"""
    g = cfg["guardrails"]
    max_tot = float(g.get("max_total_notional_pct") or g.get("max_total_exposure_pct", 60) * float(ledger.swap_cfg(cfg)["leverage_max"]))
    ex = sz_info.get("exposure") or {}
    now = float(ex.get("long") or 0) + float(ex.get("short") or 0)
    return {"tm_decides": add_position.tm_decides(cfg), "total_room": eq * max_tot / 100 - now,
            "max_total_pct": max_tot, "total_now": now}


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
                eq_ = float(bk["live_equity"] if bk.get("live_equity") else bk["equity"])
                a = add_position.budget(rc, pos, px, float(plan["stop"]), sz_info, eq_,
                                        float(cfg["guardrails"]["max_position_pct"]),
                                        float(cfg["guardrails"].get("min_order_usd", 0)), legs=legs,
                                        **_add_hard_kw(cfg, sz_info, eq_))
                it.update(existing_risk=a.get("existing_risk"), add_risk=a.get("add_risk"), cap=a.get("cap"),
                          clipped=a.get("clipped"), clip_by=a.get("clip_by"), tm_decides=a.get("tm_decides"),
                          hard_max_notional=a.get("hard_max_notional"), hard_limit=a.get("hard_limit"),
                          hard_max_sz=okx_demo.contracts(inst, float(a.get("hard_max_notional") or 0) / px) or "0",
                          margin_after_pct=a.get("margin_after_pct"), total_after_pct=a.get("total_after_pct"),
                          max_total_pct=a.get("max_total_pct"))
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
                  f" / 预算上限 {float(it['cap']):.2f} USDT（单笔风险预算"
                  + ("内" if it.get("within_budget") else "超出") + ("，仅参考不否决，是否加仓由你决定" if it.get("tm_decides") else "")
                  + (f"，已削减：{'、'.join(it.get('clip_by') or [])}" if it.get("clipped") else "") + "）")
        if it.get("hard_max_notional") is not None:
            s += (f"、按硬护栏最多还能加 {it.get('hard_max_sz') or '0'} 张（名义 {float(it['hard_max_notional']):.2f} USDT，受 {it.get('hard_limit')} 约束）")
        if it.get("margin_after_pct") is not None:
            s += f"、加仓后本币保证金占权益 {float(it['margin_after_pct']):.2f}%"
        if it.get("total_after_pct") is not None:
            s += f"、总名义占权益 {float(it['total_after_pct']):.2f}%（上限 {float(it.get('max_total_pct') or 0):g}%）"
        if it.get("reason"):
            s += f"；算不出/不可加：{it['reason']}"
        rows.append(s)
    return head + "：" + ("；".join(rows) if rows else "无可预览的同向入场价")


def _pm_execute_demo_locked(cfg, coin, did, st, w, mk, bk, pos, rationale, source="daily", tm=None, tm_state=None) -> dict:
    """OKX 模拟盘直连版组合经理执行（禁止实盘，见 desk/demo_exec.py）。交易经理下单工具给出动作 → _pm_execute_tool 按工具参数执行；
    交易经理否决/缺席（无结论）→ 不开仓、不加仓、不挂单，按 cancel_pending_on_new_decision 撤该币未受保护的旧条件单。"""
    from desk import demo_exec as DX
    inst = coin["inst_id"]
    _ta = (getattr(tm, "_order_tool", None) or {}).get("action") if tm is not None else None
    if _ta and (tm_state not in ("veto", "absent") or _ta in ("cancel_pending", "hold", "close", "reduce")):
        return _pm_execute_tool(cfg, coin, did, st, w, mk, bk, pos, rationale, source, tm, tm_state)
    agg = aggregate(cfg, st, w, mk)
    notes, execd, pend = list(agg["notes"]), None, None
    cancel_ok = DX.ecfg(cfg)["cancel_pending_on_new_decision"]
    veto = tm_state == "veto"
    action = "hold(交易经理否决)" if veto else "hold(交易经理缺席)"
    try:
        if pos and pos.get("legacy"):
            notes.append("该币是旧纸面仓（由旧仓盯盘管理止损/止盈/强平），本流程不操作、不加仓")
            action = "hold"
        elif pos:
            notes.append(("交易经理否决" if veto else "交易经理缺席") + "：不加仓，持仓保持")
            if cancel_ok:
                DX.cancel_triggers(cfg, inst, "已有持仓")
        else:
            notes.append(TM_VETO_NOTE if veto else TM_ABSENT_NOTE)
            # R9：缺席与否决一致，都撤该币未受保护的条件单（缺席原因 tm_absent）
            if cancel_ok:
                gone = DX.cancel_triggers(cfg, inst, "交易经理否决" if veto else "tm_absent")
                if gone:
                    notes.append(("交易经理否决" if veto else "交易经理缺席(tm_absent)") + "，撤旧条件单 " + "、".join(gone))
    except Exception as e:
        from desk import okx_demo
        okx_demo.log_event("exec_error", inst_id=inst, decision_id=did, error=str(e))
        notes.append(f"模拟盘执行失败：{type(e).__name__}: {e}")
    return {"action": action, "size_pct": 0.0, "notes": notes, "execution": execd, "aggregate": agg, "pending": pend}


def tool_levels(agg, tm, mk, ex) -> str | None:
    """把交易经理下单工具的方向与点位写进 agg（覆盖加权参考点位），返回下单方向。"""
    if tm is None:
        return agg.get("side")
    side = tm.entry_side if tm.action == "pending" else ("long" if tm.action == "buy" else "short" if tm.action == "short" else agg.get("side"))
    if tm.action in ("buy", "short"):
        agg.update(side=side, condition="now", entry_px=float(mk["last"]), stop_loss=float(tm.stop_loss),
                   take_profit=float(tm.take_profit), plan_group="tm_order_tool", buy=True)
    elif tm.action == "pending":
        agg.update(side=side, condition=tm.entry_condition or (ex or {}).get("condition"), entry_px=float(tm.entry_trigger_px),
                   stop_loss=float(tm.entry_stop_loss), take_profit=float(tm.entry_take_profit), plan_group="tm_order_tool", buy=True)
    agg["horizon_days"] = int(tm.horizon_days or agg.get("horizon_days") or 5)
    return side


def _tool_contracts(cfg, inst, bk, tm, agg, mk, px, sl):
    """下单工具定张数：给了 margin_pct 以交易经理为准（不按风险重算），否则按现有 demo_size×risk_scale。
    返回 (sz, lev, size_pct, info) 或 (None, ..., 原因)。硬护栏仍由执行函数里的 check_open / clip_to_risk 把关。"""
    from desk import config_safety, okx_demo as X
    mp = (tm._order_tool or {}).get("margin_pct")
    lev = float(min(max(float(tm.leverage or 1), 1), config_safety.max_open_leverage(cfg)))
    if mp:
        eq = float(bk.get("live_equity") or bk["equity"])
        notional = mp / 100 * eq * lev
        info = {"ok": True, "how": "tm_margin_pct", "margin_pct": mp, "notional": round(notional, 2), "leverage": lev}
    else:
        r = demo_size(cfg, {**agg, "entry_px": px, "stop_loss": sl}, mk, bk, tm, inst)
        if not r.get("ok"):
            return None, lev, 0.0, {"ok": False, "reason": r.get("reason")}
        notional, lev = float(r["notional"]), float(r["leverage"])
        info = {"ok": True, "how": "risk_sizing", "risk_scale": tm.risk_scale, "notional": round(notional, 2), "leverage": lev}
    sz = X.contracts(inst, notional / px)
    size_pct = notional / lev / float(bk["equity"]) * 100
    info.update(sz=sz, size_pct=round(size_pct, 4))
    return sz, lev, size_pct, info


def _pm_execute_tool(cfg, coin, did, st, w, mk, bk, pos, rationale, source, tm, tm_state) -> dict:
    """manager.tm_order_tool：按交易经理下单工具参数原样执行（参数已在 tm_order_tool.validate 预检过硬护栏）。
    点位、条件、张数（margin_pct）、保留/重挂一律以交易经理为准；执行函数内的 check_open / clip_to_risk / 冷却 / 漂移 /
    执行前复核仍作为最后硬护栏（预检通过时不应再触发）。只对 OKX 模拟盘下单。"""
    from desk import demo_exec as DX, okx_demo
    inst = coin["inst_id"]
    t = tm._order_tool or {}
    a = t.get("action")
    try:
        ex = DX.existing_trigger(bk, inst)
    except Exception:
        ex = None
    agg = aggregate(cfg, st, w, mk)
    notes, execd, pend, action, size = list(agg["notes"]), None, None, "hold", 0.0
    cancel_ok = DX.ecfg(cfg)["cancel_pending_on_new_decision"]
    notes.append(f"交易经理下单工具：action={a}（参数以交易经理为准，代码只过硬护栏）")
    S_side = agg.get("side") if agg.get("buy") else None
    side = tool_levels(agg, tm, mk, ex)
    # 止盈后再讨论：交易经理条件单方向与共识相反 → 观望不下单（方向闸门之外的保险）
    tp_hold = str(source).startswith("post_tp") and tm.action == "pending" and S_side is not None and side != S_side
    if tp_hold:
        notes.append("止盈后再讨论：交易经理条件单方向与共识相反 → 观望不下单")
    agg["tm_order_tool"] = {"action": a, "margin_pct": t.get("margin_pct")}
    try:
        if pos and pos.get("legacy"):
            notes.append("该币是旧纸面仓（由旧仓盯盘管理止损/止盈/强平），本流程不操作、不加仓")
        elif a == "cancel_pending":
            gone = DX.cancel_triggers(cfg, inst, "交易经理撤单")
            notes.append("交易经理 cancel_pending：撤条件单 " + ("、".join(gone) if gone else "（无可撤，受保护的不撤）"))
            action = "hold(交易经理撤单)"
        elif a == "close":
            execd = DX.close(cfg, pos, "经理决策清仓"); action = "sell"
        elif a == "reduce":
            f = float(tm.reduce_fraction)
            execd = DX.reduce(cfg, pos, f, f"经理决策减仓{f:.0%}"); action = "reduce"
        elif a == "hold":
            action = "hold"
            notes.append("交易经理 hold：不开仓、不动挂单" + ("（持仓按 new_stop/new_tp 调点，留空保持原值）" if pos else ""))
        elif tp_hold:
            action = "hold(止盈后不追单)"
        elif (a == "open" and pos) or (a == "add" and not pos):
            # 锁内重读账户后与工具动作不符（如并发已开仓）→ 不执行，等下次讨论
            action = "hold(持仓已变化)"
            notes.append(f"锁内重读账户：交易经理 {a} 时{'已有持仓' if pos else '已无持仓'}，本次不执行")
        elif _cooldown(cfg, inst, agg, did, notes):
            action = "hold(止损冷却)"
        elif a == "keep_pending":
            # 保留现有挂单：不撤、不重算张数，只按交易经理止损止盈（过 pending_keep_sltp 硬护栏）与有效期更新
            pend = keep_same_trigger(cfg, inst, mk, agg, ex, tm, notes, bk); action = "pending_add" if pos else "pending"
            pend["kept_sz"] = ex.get("sz")
            notes.append(f"交易经理 keep_pending：保留现有条件单 {ex['algoId']}，张数 {ex.get('sz')} 不变")
            exp_chg = apply_tm_expiry(cfg, inst, pend.get("algoId") or ex["algoId"], tm, notes)
            if exp_chg:
                pend["expires_changed"] = exp_chg
        elif a in ("open", "add") and drift.skip(cfg, inst, did, mk, agg, notes, "add" if pos else "open"):
            action = "hold(价格漂移)"
        else:
            gate = pre_trade_gate(cfg, inst, mk)
            px, sl, tp = float(agg["entry_px"]), float(agg["stop_loss"]), float(agg["take_profit"])
            sz, lev, size, info = (None, 1.0, 0.0, {"ok": False, "reason": gate}) if gate else \
                _tool_contracts(cfg, inst, bk, tm, agg, mk, px, sl)
            agg["sizing"] = info
            if gate:
                notes.append(f"执行前复核未通过（{gate}），本次不下单")
            elif not info.get("ok") or not sz:
                notes.append(f"定张数失败：{info.get('reason') or '不足最小张数'}"); size = 0.0
            else:
                notes.append(f"张数 {sz}（{'交易经理 margin_pct ' + format(info['margin_pct'], 'g') + '%' if info['how'] == 'tm_margin_pct' else '按风险定仓×risk_scale ' + format(float(tm.risk_scale), 'g')}，"
                             f"名义 {info['notional']:.2f}，杠杆 {lev:g}x）")
                if a == "replace_pending":
                    gone = DX.cancel_triggers(cfg, inst, "交易经理重挂")
                    notes.append(f"交易经理 replace_pending：撤旧条件单 {'、'.join(gone) or '无'}，按新参数重挂")
                if pos:
                    eq_ = float(bk["live_equity"] if bk.get("live_equity") else bk["equity"])
                    meta = {"base_contracts": pos["contracts"], "base_avg": pos["avg_px"], "base_sl": float(pos["sl_px"]) if pos.get("sl_px") else None,
                            "base_tp": pos.get("tp_px"), "base_qty": pos["qty"], "add_px": px, "risk_cap_usd": DX.risk_cap_usd(cfg, eq_),
                            "final_sl": sl, "final_tp": tp, "tm_decides": True, "tm_order_tool": True, "algoId": None}
                    if a == "add":
                        ok, inf = DX.add_now(cfg, bk, inst, pos, sz, sl, tp, did, px, {"tm_decides": True})
                        execd = {"type": "add", "ok": ok, **inf}
                        action = ("add_long" if side == "long" else "add_short") if ok else "hold(护栏拒单)"
                        if not ok:
                            notes.append("加仓护栏拒单：" + "；".join(inf["reasons"]))
                    else:
                        ok, inf = DX.place_add_trigger(cfg, bk, inst, pos, {
                            "decision_id": did, "source": source, "side": side, "trigger_px": px, "condition": agg["condition"],
                            "stop_loss": sl, "take_profit": tp, "size_pct": 0, "horizon_days": agg["horizon_days"],
                            "expire_hours": float(tm.entry_expire_hours), "consensus": agg["S"],
                            "reason": ("同向加仓：" + (tm.rationale or ""))[:200]}, sz, meta)
                        if ok:
                            pend = inf; action = "pending_add"
                        else:
                            action = "hold(护栏拒单)"; notes.append("条件加仓单护栏拒单：" + "；".join(inf["reasons"]))
                elif a == "open":
                    DX.cancel_triggers(cfg, inst, "改为现价成交")
                    ok, inf = DX.open_now(cfg, bk, inst, size, sl, tp, side, lev, did, agg["horizon_days"], mk["last"])
                    execd = {"type": "open", "ok": ok, **inf}
                    action = ("buy" if side == "long" else "short") if ok else "hold(护栏拒单)"
                    if not ok:
                        notes.append("护栏拒单：" + "；".join(inf["reasons"]))
                else:
                    ok, inf = DX.place_trigger(cfg, bk, inst, {
                        "decision_id": did, "source": source, "side": side, "leverage": lev, "trigger_px": px,
                        "condition": agg["condition"], "stop_loss": sl, "take_profit": tp, "size_pct": size,
                        "horizon_days": agg["horizon_days"], "expire_hours": float(tm.entry_expire_hours), "consensus": agg["S"],
                        "reason": (tm.rationale or rationale or "")[:200]})
                    if ok:
                        pend = inf; action = "pending"
                        notes.append(f"模拟盘条件单 {inf['algoId']}：{'多' if side == 'long' else '空'} {lev:g}x "
                                     f"{'≥' if agg['condition'] == 'gte' else '≤'} {px:.6g}，止损 {sl:.6g}，止盈 {tp:.6g}，"
                                     f"{inf.get('sz')} 张，{float(tm.entry_expire_hours):.0f} 小时有效")
                    else:
                        action = "hold(护栏拒单)"; notes.append("护栏拒单：" + "；".join(inf["reasons"]))
                if isinstance(execd, dict) and execd.get("sz") and str(execd["sz"]) != str(sz):
                    notes.append(f"硬护栏封顶：张数 {sz}→{execd['sz']}")
        if pos and cancel_ok and action in ("sell", "reduce") and ex:
            DX.cancel_triggers(cfg, inst, "已有持仓")
    except Exception as e:
        okx_demo.log_event("exec_error", inst_id=inst, decision_id=did, error=str(e))
        notes.append(f"模拟盘执行失败：{type(e).__name__}: {e}")
        action = "hold(执行失败)" if action == "hold" else action
    return {"action": action, "size_pct": size, "notes": notes, "execution": execd, "aggregate": agg, "pending": pend}
