"""行情判定（2026-11-11 用户决定：以 LLM 判定取代 A+ 的代码机械判定 trend_regime）。

分工：代码只算数据——趋势特征表（均线/斜率/偏离/RSI/区间位置，均由已收盘日线经现有
okx_data 路径计算）；判定由与交易经理同一模型席位（role_models.trade_manager）的 LLM 在每场
TM 决策前做一次，输出严格结构化结果。代码再按判定执行趋势市近价护栏与末段衰竭降级
（desk/tm_order_tool.validate §3.5），判定本身不进共识、不影响否决口径。
判定失败/不可解析 → 本场本币按「非趋势市」处理（规则不生效），决策记录记 regime_judge_failed
数据缺口，绝不因判定失败中断场次。总开关 manager.trend_rule_enabled=false 时整段关闭
（不调判定、不注入提示词、护栏同时失效，回到 A+ 之前行为）。
每次判定（含特征快照）另存 state/ledger/regime_calls.jsonl，供日后与实际走势对照打分（打分未建）。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from desk import okx_data, settings
from desk import llm as dllm

REGIME_CALLS = settings.STATE / "ledger" / "regime_calls.jsonl"
FEATURE_BARS = 260          # MA200 与其 5 日斜率需要 ≥205 根已收盘日线，取 260 留余量


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
    stage: Literal["early", "mid", "late_exhaustion"] = Field(description="趋势阶段：early=刚确立，mid=进行中，late_exhaustion=末段衰竭（走势拉伸、贴极值、偏离过深）")
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
    return {"direction": d["direction"], "strength": d["strength"], "stage": d["stage"],
            "confidence": max(0.0, min(1.0, float(conf))),
            "evidence": [str(x)[:120] for x in list(ev)[:8]],
            "summary": str(d.get("summary") or "")[:120]}


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
              "- stage：early=趋势刚确立；mid=趋势进行中；late_exhaustion=末段衰竭——做空看：现价贴近 20 日低点/区间底部、"
              "RSI 极低、短期跌幅过大、现价对均线偏离过深（做多镜像：贴近高点、RSI 极高、偏离过高），"
              "此时继续追单边容易被反向波动打穿。",
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
def build_regime(cfg, judge, feat) -> dict:
    """把判定结果整理成 validate/提示词/决策记录共用的 regime dict。
    规则生效（active）= 方向为 long/short 且强度 strong/moderate；late_exhaustion 在生效时即降级。"""
    tc = trend_cfg(cfg)
    active = bool(judge and judge["direction"] in ("long", "short") and judge["strength"] in ("strong", "moderate"))
    return {"enabled": tc["enabled"], "source": "llm", "failed": judge is None,
            "judge": judge, "features": feat, "cfg": tc, "active": active,
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
           "inst_id": inst, "decision_id": decision_id,
           "judge": regime.get("judge"), "failed": bool(regime.get("failed")),
           "active": bool(regime.get("active")), "downgrade": bool(regime.get("downgrade")),
           "features": regime.get("features")}
    settings.append_jsonl(REGIME_CALLS, rec)
    return rec
