"""行情判定（2026-11-11 用户决定：以 LLM 判定取代 A+ 的代码机械判定 trend_regime）。

分工：代码只算数据——趋势特征表（均线/斜率/偏离/RSI/区间位置，均由已收盘日线经现有
okx_data 路径计算）；判定由与交易经理同一模型席位（role_models.trade_manager）的 LLM 在每场
TM 决策前做一次，输出严格结构化结果。代码再按判定执行趋势市近价护栏与末段衰竭降级
（desk/tm_order_tool.validate §3.5），判定本身不进共识、不影响否决口径。
判定失败/不可解析 → 本场本币按「非趋势市」处理（规则不生效），决策记录记 regime_judge_failed
数据缺口，绝不因判定失败中断场次。总开关 manager.trend_rule_enabled=false 时整段关闭
（不调判定、不注入提示词、护栏同时失效，回到 A+ 之前行为）。
每次判定（含特征快照）另存 state/ledger/regime_calls.jsonl，供日后与实际走势对照打分（打分未建）。

判定标准 v2（judge_standard=2，2026-11-14 用户批准）：审计发现 v1 标准下 18 次场次判定 16 次
late_exhaustion、事后 8 次独立观测 8/8 沿趋势延续、衰竭证据恒收敛于 RSI 超卖/区间底部/偏离过深/
短期跌幅四个「伸展变量」，肯定式衰竭证据引用 0/18——stage 边界实质是拉伸程度阈值而非趋势失败
检验。v2 改为：① late_exhaustion 必须给出正面衰竭证据 exhaustion_signals（五项闭集，见下）；
② RSI/区间位置/偏离/短期涨跌幅归类为伸展变量，只支持 direction/strength，不许推出 late；
③ early/mid 给正面定义，结构完整时默认 mid；④ 代码门（apply_stage_gate）：返回 late 但
exhaustion_signals 无任何闭集内有效值 → 生效 stage 强制降为 mid，原始 stage 与覆盖标记随判定
一并存档——纯提示词约束已被审计证明会被模型绕过（自述「动量未衰竭」仍判衰竭），故由代码裁定。
v1 存档记录无 judge_standard 字段，打分时按 v1 分段。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from desk import okx_data, settings
from desk import llm as dllm

REGIME_CALLS = settings.STATE / "ledger" / "regime_calls.jsonl"
FEATURE_BARS = 260          # MA200 与其 5 日斜率需要 ≥205 根已收盘日线，取 260 留余量
JUDGE_STANDARD = 2          # 判定标准版本（v1=2026-11-11 上线版，无此字段；v2=2026-11-14 衰竭证据门）

# 正面衰竭证据闭集（v2）：stage=late_exhaustion 时必须至少给出一项（代码侧校验，闭集外值无效）。
EXHAUSTION_SIGNALS = ("slope_flattening", "deepening_pullbacks", "structure_break",
                      "momentum_divergence", "volume_stall")
_EXHAUSTION_CN = {"slope_flattening": "均线斜率收敛/走平", "deepening_pullbacks": "反弹/回撤深度递增",
                  "structure_break": "高低点结构破坏", "momentum_divergence": "动量背离",
                  "volume_stall": "放量滞跌/滞涨"}


def trend_cfg(cfg) -> dict:
    """趋势市规则配置（manager 段）。判定改 LLM 后只剩总开关、近价距离与降级风险上限；
    A+ 的代码判定阈值（trend_consensus/trend_min_votes/trend_ret20d_pct/超卖两条）已删除。"""
    M = (cfg or {}).get("manager") or {}
    return {"enabled": bool(M.get("trend_rule_enabled", True)),
            "near_atr": float(M.get("trend_near_atr", 0.3)),
            "oversold_risk_scale": float(M.get("trend_oversold_risk_scale", 0.5))}


# ------------------------------------------------------------------ 趋势特征（只算数据，不下结论）
def _ma_stats(close, window: int):
    """(均线现值, 均线 5 日斜率%, 现价相对均线偏离%)；样本不足对应项为 None。"""
    if len(close) < window:
        return None, None, None
    ma = close.rolling(window).mean()
    cur = float(ma.iloc[-1])
    slope = None
    if len(close) >= window + 5:
        prev = float(ma.iloc[-6])
        if prev:
            slope = round((cur / prev - 1) * 100, 3)
    dev = round((float(close.iloc[-1]) / cur - 1) * 100, 3) if cur else None
    return round(cur, 4), slope, dev


def _rsi14(close) -> float | None:
    """Wilder RSI14（ewm alpha=1/14）；样本不足返回 None，全程无涨跌记 50。"""
    if len(close) < 15:
        return None
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    u, v = float(up.iloc[-1]), float(dn.iloc[-1])
    if v == 0:
        return 100.0 if u > 0 else 50.0
    return round(100 - 100 / (1 + u / v), 2)


def features(inst: str) -> dict:
    """趋势特征表：从已收盘日线（okx_data.candles 现有缓存/节流路径，一次取回 ≥300 根）计算。
    与 manager.market_ctx 的 5/20 日涨跌幅、ATR、20 日高低同源同口径，渲染时从 mk 取。"""
    d = okx_data.candles(inst, "1Dutc", FEATURE_BARS)
    c, h, l = d.Close, d.High, d.Low
    out: dict = {"bars": int(len(d))}
    for w in (20, 50, 200):
        v, slope, dev = _ma_stats(c, w)
        out[f"ma{w}"], out[f"ma{w}_slope_pct"], out[f"dev_ma{w}_pct"] = v, slope, dev
    out["rsi14"] = _rsi14(c)
    hi20, lo20, last = float(h.tail(20).max()), float(l.tail(20).min()), float(c.iloc[-1])
    out["pos_in_range"] = round((last - lo20) / (hi20 - lo20), 4) if hi20 > lo20 else None
    return out


def _num(v) -> str:
    return "数据不足" if v is None else f"{v:.6g}"


def _pct(v) -> str:
    return "数据不足" if v is None else f"{v:+.2f}%"


def feature_block(feat, mk) -> str:
    """趋势特征渲染：判定提示词与交易经理提示词共用同一块，保证两边看到的数据完全一致。"""
    f = feat or {}

    def ma_seg(label: str, key: str) -> str:
        return (f"{label} {_num(f.get(key))}（5 日斜率 {_pct(f.get(key + '_slope_pct'))}，"
                f"现价偏离 {_pct(f.get('dev_' + key + '_pct'))}）")
    pos = f.get("pos_in_range")
    pos_s = "数据不足" if pos is None else f"{pos * 100:.0f}%"

    def mkn(key, pct=False):   # mk 字段缺失（旧记录/测试桩）时渲染为「数据不足」，不抛错
        v = (mk or {}).get(key)
        return "数据不足" if v is None else (f"{float(v):+.2f}%" if pct else f"{float(v):.6g}")
    return ("## 趋势特征（代码计算，仅数据，不含结论）\n"
            f"现价 {mkn('last')}；{ma_seg('MA20', 'ma20')}；{ma_seg('MA50', 'ma50')}；{ma_seg('MA200', 'ma200')}；"
            f"RSI14 {_num(f.get('rsi14'))}；20 日区间位置 {pos_s}（高 {mkn('hi_20')} / 低 {mkn('lo_20')}）；"
            f"5 日 {mkn('ret_5d', pct=True)}、20 日 {mkn('ret_20d', pct=True)}；"
            f"ATR14 {mkn('atr14')}（基于 {f.get('bars', '?')} 根已收盘日线）")


# ------------------------------------------------------------------ LLM 行情判定
class RegimeJudgment(BaseModel):
    direction: Literal["short", "long", "none"] = Field(description="趋势方向：short=空头趋势，long=多头趋势，none=无趋势/震荡")
    strength: Literal["strong", "moderate", "none"] = Field(description="趋势强度：strong=多项特征同向且显著，moderate=同向但一般；direction=none 时填 none")
    stage: Literal["early", "mid", "late_exhaustion"] = Field(description="趋势阶段：early=刚确立，mid=进行中，late_exhaustion=末段衰竭。"
        "late_exhaustion 只许在 exhaustion_signals 至少给出一项正面衰竭证据时返回；RSI 极端、贴区间边缘、偏离均线深、"
        "短期涨跌幅大都是伸展变量，在强趋势中是常态，只支持 direction/strength，永远不构成衰竭证据")
    exhaustion_signals: list[str] = Field(default_factory=list, description="正面衰竭证据（闭集，可多选；stage 非 late_exhaustion 时留空）："
        "slope_flattening=均线斜率收敛/走平；deepening_pullbacks=反弹/回撤深度递增；structure_break=高低点结构破坏；"
        "momentum_divergence=动量背离实际出现（evidence 中须指明哪个指标与价格背离）；volume_stall=放量滞跌/滞涨。"
        "stage=late_exhaustion 时必须非空且只能取上述值，否则代码校验会把 stage 强制降为 mid")
    confidence: float = Field(description="0~1，对本次行情判定的置信度")
    evidence: list[str] = Field(description="支撑判定的证据，每条引用具体特征值（如「MA50 斜率 -1.23%」「RSI14 31.2」）")
    summary: str = Field(description="一句话中文结论（≤60 字）")


_DIRS, _STRS, _STGS = ("short", "long", "none"), ("strong", "moderate", "none"), ("early", "mid", "late_exhaustion")


def normalize_judgment(obj) -> dict | None:
    """结构化输出（RegimeJudgment 实例或 dict）→ 归一化 dict；不合格返回 None（按判定失败处理）。"""
    try:
        if isinstance(obj, RegimeJudgment):
            d = obj.model_dump()
        elif isinstance(obj, dict):
            obj = dict(obj)
            if isinstance(obj.get("evidence"), str):   # 模型偶尔把 evidence 给成单个字符串，先包成列表再校验
                obj["evidence"] = [obj["evidence"]]
            if isinstance(obj.get("exhaustion_signals"), str):
                obj["exhaustion_signals"] = [obj["exhaustion_signals"]]
            d = RegimeJudgment(**obj).model_dump()
        else:
            return None
    except Exception:
        return None
    if d["direction"] not in _DIRS or d["strength"] not in _STRS or d["stage"] not in _STGS:
        return None
    conf = d.get("confidence")
    if not isinstance(conf, (int, float)) or conf != conf:   # 非数值或 NaN
        return None
    ev = d.get("evidence") or []
    if isinstance(ev, str):
        ev = [ev]
    sig_raw_in = d.get("exhaustion_signals") or []
    if isinstance(sig_raw_in, str):
        sig_raw_in = [sig_raw_in]
    elif not isinstance(sig_raw_in, (list, tuple)):
        sig_raw_in = [sig_raw_in]
    sig_raw = [str(x)[:40] for x in list(sig_raw_in)[:8]]
    sig_valid = list(dict.fromkeys(s for s in sig_raw if s in EXHAUSTION_SIGNALS))   # 闭集过滤+保序去重
    return {"direction": d["direction"], "strength": d["strength"], "stage": d["stage"],
            "confidence": max(0.0, min(1.0, float(conf))),
            "evidence": [str(x)[:120] for x in list(ev)[:8]],
            "summary": str(d.get("summary") or "")[:120],
            "exhaustion_signals": sig_valid,
            "exhaustion_signals_raw": sig_raw,
            "judge_standard": JUDGE_STANDARD}


def judge_prompt(coin_name: str, inst: str, feat, mk, S: float, votes: dict, market_text: str) -> str:
    lines = [f"你是加密货币交易台的行情判定员。你只做一件事：判断 {coin_name}（OKX 永续 {inst}）当前处于什么行情状态。",
             "你不做交易决策、不给买卖点位；只依据下面给出的数据下判定，结论必须能在 evidence 里引用到具体特征值。",
             "",
             feature_block(feat, mk),
             "",
             f"团队共识（仅供参考，不是判定依据本身）：加权共识 S={S:+.3f}；有效投票 {votes.get('valid', 0)} 个，"
             f"其中看多 {votes.get('long', 0)}、看空 {votes.get('short', 0)}、中性 {votes.get('neutral', 0)}。"]
    if market_text:
        lines += ["", "## 市场分析师结论摘录（供参考）", market_text]
    lines += ["", "判定口径：",
              "- direction：多项特征一致指向单边行情时给 short/long；方向混杂、均线纠缠、震荡特征明显时给 none。",
              "- strength：strong=多项特征同向且幅度显著；moderate=同向但幅度一般、或个别特征不一致；direction=none 时 strength=none。",
              "- stage 的两类变量必须分清（v2 标准）：",
              "  · 伸展变量：RSI14 水平、20 日区间位置、现价偏离均线的距离、5 日/20 日涨跌幅。它们只说明行情已经走了多远。"
              "强趋势中 RSI 长期钉在极值区、价格贴着区间边缘运行是常态（强下跌里 RSI 20 上下、贴区间底可以持续很多天），"
              "因此伸展变量只许用来支撑 direction 与 strength，单独或任意组合都不许推出 late_exhaustion。",
              "  · 衰竭证据：只有出现「趋势正在失败」的正面信号，才谈得上末段衰竭（见 stage 定义的闭集）。",
              "- stage（正面定义）：",
              "  · early=趋势刚确立：均线刚转为同向排列、斜率刚转向趋势方向、现价与均线距离尚近、顺趋势的新极值刚开始出现。",
              "  · mid=趋势进行中且结构完整：均线斜率稳定或还在加深、回调浅且很快被趋势方向重新吞没、高低点结构沿趋势方向推进"
              "（下跌看更低的低点/高点，上涨镜像）。只要结构未破，即使伸展变量已经很极端，也判 mid，不要因为「跌多了/涨多了」改判衰竭。",
              "  · late_exhaustion=末段衰竭：必须至少出现一项正面衰竭证据，并把选中的项填入 exhaustion_signals（只能取下列闭集值，可多选），"
              "同时在 evidence 中为每一项写明对应的具体特征数值：",
              "    slope_flattening=均线斜率收敛/走平（以单时点近似：趋势方向上 MA20 斜率绝对值已明显小于 MA50 斜率、或斜率绝对值本身接近 0）；",
              "    deepening_pullbacks=逆趋势反弹/回撤的深度一轮比一轮深；",
              "    structure_break=高低点结构破坏（下跌趋势出现更高的低点并站稳、或更高的高点；上涨镜像）；",
              "    momentum_divergence=动量背离实际出现（evidence 须指明哪个指标与价格如何背离，如「价格创新低而 RSI14 未创新低」；"
              "只写「无背离」或指标未提及不算）；",
              "    volume_stall=放量滞跌/滞涨（量能放大但价格无法继续沿趋势方向推进）。",
              "  · 若拿不到任何一项衰竭证据，stage 不许给 late_exhaustion；系统有代码校验：返回 late_exhaustion 但 exhaustion_signals "
              "为空或含闭集外的值，生效 stage 会被强制降为 mid 并记入存档。",
              "只输出结构化结果，不要输出其他文字。"]
    return "\n".join(lines)


def _judge_model(cfg, tracker):
    """与交易经理同一模型席位（role_models.trade_manager）；不套交易经理手册——判定是独立职责，
    与立场抽取（stances.extract）一样直接用结构化输出。"""
    return dllm.make_llm(cfg, "deep", tracker, role="trade_manager").with_structured_output(RegimeJudgment)


def call_judge(cfg, tracker, prompt: str) -> dict | None:
    """调用判定：底层 BaseChatModel.invoke 已有 llm_retry 退避；此处对解析/校验失败再重试一次，
    仍失败返回 None（上层按判定失败处理）。模型构建失败同样返回 None。"""
    try:
        model = _judge_model(cfg, tracker)
    except Exception:
        return None
    for _ in range(2):
        try:
            out = model.invoke(prompt)
        except Exception:
            continue
        j = normalize_judgment(out)
        if j is not None:
            return j
    return None


# ------------------------------------------------------------------ 判定结果 → 规则/提示词/日志
def apply_stage_gate(judge) -> dict | None:
    """v2 代码门：stage=late_exhaustion 但 exhaustion_signals 无任何闭集内有效值（normalize 已做闭集
    过滤，此处只看过滤后的列表）→ 生效 stage 强制降为 mid。返回标注后的判定副本：stage=生效值、
    stage_raw=模型原始值、stage_overridden 标记、exhaustion_signals 一律为闭集内有效值（防御性再过滤
    一次，未经 normalize 的直构 dict 也受同一门约束）。原始判定不丢：raw 与覆盖动作都随判定进存档。"""
    if not judge:
        return None
    j = dict(judge)
    sig = [s for s in (j.get("exhaustion_signals") or []) if s in EXHAUSTION_SIGNALS]
    j["exhaustion_signals"] = list(dict.fromkeys(sig))
    raw = j.get("stage")
    j["stage_raw"] = raw
    if raw == "late_exhaustion" and not j["exhaustion_signals"]:
        j["stage"] = "mid"
        j["stage_overridden"] = True
        j["stage_override_reason"] = "late_exhaustion_without_exhaustion_signals"
    else:
        j["stage_overridden"] = False
        j.pop("stage_override_reason", None)
    j.setdefault("judge_standard", JUDGE_STANDARD)
    return j


def build_regime(cfg, judge, feat) -> dict:
    """把判定结果整理成 validate/提示词/决策记录共用的 regime dict。judge 先过 v2 代码门
    （apply_stage_gate），此后所有消费者（降级、移动止损资格、提示词、存档）只见生效 stage。
    规则生效（active）= 方向为 long/short 且强度 strong/moderate；late_exhaustion 在生效时即降级。"""
    tc = trend_cfg(cfg)
    judge = apply_stage_gate(judge)
    active = bool(judge and judge["direction"] in ("long", "short") and judge["strength"] in ("strong", "moderate"))
    return {"enabled": tc["enabled"], "source": "llm", "failed": judge is None,
            "judge": judge, "features": feat, "cfg": tc, "active": active,
            "judge_standard": JUDGE_STANDARD,
            "side": judge["direction"] if active else None,
            "downgrade": bool(active and judge["stage"] == "late_exhaustion")}


_DIR_CN = {"short": "空头", "long": "多头", "none": "无趋势"}
_STR_CN = {"strong": "强", "moderate": "中等", "none": "无"}
_STAGE_CN = {"early": "早期", "mid": "中段", "late_exhaustion": "末段衰竭"}


def regime_block(regime: dict, mk) -> str:
    """交易经理提示词里的「本场行情判定（LLM）」段：判定结果 + 依判定生效的执行规则。"""
    tc = regime["cfg"]
    if regime.get("failed") or not regime.get("judge"):
        return ("## 本场行情判定（LLM）\n"
                "判定失败（regime_judge_failed）：本场趋势市执行规则不生效，按常规规则决策。")
    j = regime["judge"]
    head = (f"## 本场行情判定（LLM，与交易经理同一模型席位，决策前判定）\n"
            f"方向={_DIR_CN[j['direction']]}（{j['direction']}）；强度={_STR_CN[j['strength']]}（{j['strength']}）；"
            f"阶段={_STAGE_CN[j['stage']]}（{j['stage']}）；置信 {j['confidence']:.2f}。\n"
            f"结论：{j['summary']}\n"
            "证据：" + ("；".join(j["evidence"]) if j["evidence"] else "（无）"))
    if j.get("stage_overridden"):
        head += ("\n注：判定员原始阶段为末段衰竭（late_exhaustion），但未给出任何正面衰竭证据"
                 "（exhaustion_signals 为空），已按判定标准 v2 代码校验强制降为中段（mid）生效，末段衰竭降级不触发。")
    elif j["stage"] == "late_exhaustion" and j.get("exhaustion_signals"):
        head += ("\n衰竭证据（正面信号）：" + "；".join(
            f"{_EXHAUSTION_CN.get(s, s)}（{s}）" for s in j["exhaustion_signals"]))
    if not regime.get("active"):
        return head + "\n本场趋势市执行规则不生效（判定无趋势或强度不足），按常规规则决策。"
    cap = tc["near_atr"] * float((mk or {}).get("atr14") or 0)
    s = (head + f"\n趋势市执行规则（依本场判定生效，只改入场方式，不改否决门槛）：判定方向与下单方向一致时，"
         f"禁止把唯一入场押在远离现价的位置上——必须二选一：(a) 现价入场（open）；(b) 近价挂单（pending），"
         f"触发价距现价≤{tc['near_atr']:g}×ATR14={cap:.6g}（做空：gte 小反弹触发或 lte 跌破触发均可），"
         "超出会被校验退回（[TREND_DIST]）。现价单若因价格漂移被拦，改走 (b)，不许因此改为 hold 或远位单。")
    if regime.get("downgrade"):
        s += (f"\n本场判定阶段为末段衰竭（late_exhaustion），降级生效：禁用现价 open，只许近价 pending，"
              f"且 risk_scale≤{tc['oversold_risk_scale']:g}（[TREND_OVERSOLD] 硬校验）。")
    s += ("\n趋势市里仍可 hold，但 rationale 必须写明：判定证据里哪一条不成立，或哪条硬护栏不允许下单。"
          "只写「等反弹确认」「把握不足」视为未说明，会在日报中被标出。")
    return s


def log_call(*, date, session, source, inst, decision_id, regime) -> dict:
    """每次判定追加一行 state/ledger/regime_calls.jsonl（判定 + 特征快照），供日后对照实际走势打分。"""
    rec = {"time": settings.now_str(), "date": date, "session": session, "source": source,
           "inst_id": inst, "decision_id": decision_id, "judge_standard": JUDGE_STANDARD,
           "judge": regime.get("judge"), "failed": bool(regime.get("failed")),
           "active": bool(regime.get("active")), "downgrade": bool(regime.get("downgrade")),
           "features": regime.get("features")}
    settings.append_jsonl(REGIME_CALLS, rec)
    return rec
