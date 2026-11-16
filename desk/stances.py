"""从 TradingAgents 的最终状态中取出每个角色的观点：先按手册要求的【立场】行解析，缺失的再用 LLM 抽取。"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from desk import llm as dllm
from desk.roles import ROLE_CN, ROLE_KEYS


def role_texts(fs: dict) -> dict:
    ids, rds = fs.get("investment_debate_state") or {}, fs.get("risk_debate_state") or {}
    return {
        "market": fs.get("market_report", ""), "sentiment": fs.get("sentiment_report", ""),
        "news": fs.get("news_report", ""), "flows": fs.get("_flows_report", ""), "bull": ids.get("bull_history", ""), "bear": ids.get("bear_history", ""),
        "research_manager": fs.get("investment_plan", ""), "trader": fs.get("trader_investment_plan", ""),
        "aggressive": rds.get("aggressive_history", ""), "conservative": rds.get("conservative_history", ""),
        "neutral": rds.get("neutral_history", ""), "portfolio_manager": fs.get("final_trade_decision", ""),
    }


DIR_MAP = {"看多": "bullish", "看空": "bearish", "中性": "neutral", "偏多": "bullish", "偏空": "bearish",
           "bullish": "bullish", "bearish": "bearish", "neutral": "neutral",
           "long": "bullish", "short": "bearish"}
_NUM = r"([0-9][0-9,]*\.?[0-9]*|无|N/?A|none)"
STANCE_RE = re.compile(r"【立场】\s*方向\s*[=:：]\s*(看多|看空|中性|偏多|偏空)\s*[；;,，]\s*目标价\s*[=:：]\s*" + _NUM +
                       r"\s*[；;,，]\s*止损价\s*[=:：]\s*" + _NUM + r"\s*[；;,，]\s*置信度\s*[=:：]\s*([0-9.]+)\s*[；;,，]\s*周期\s*[=:：]\s*([0-9]+)",
                       re.I)

# 解析 v2（2026-11-02）：逐字段解析。v1 的 STANCE_RE 要求五字段同行、同序、全齐，任一偏差整行作废、
# 静默掉到 LLM 粗抽。v2 改为在最后一个【立场】段内逐字段独立识别：顺序不限，目标/止损可缺省，
# 置信度可写百分比（如 65%），周期可带单位（5天/5日/5 days）。语义与 v1 一致的部分：
# 取最后一个“完整”立场（方向+置信度+周期齐全者优先，靠前的完整立场胜过靠后的残缺立场）；
# 只有残缺立场时取最后一个带方向的，置信度缺失按 0.5 计并打 partial 标记（日报解析健康可见）。
# 解析 v3（2026-11-15，P3/P4）：中英双语标签与标记（【立场】/[STANCE]、方向/dir、目标价/tgt、
# 止损价/stop、置信度/conf、周期/hz、入场价/entry、入场条件/cond、对现有挂单/pending），
# 取值同样双语（看多/long/bullish 等）。口径（用户 P3 修订）：抽取端容错——LLM 自由表达、
# 措辞可变，代码负责多模式识别，绝不因格式偏差拒绝一个角色的输出。
_DIR_RE = re.compile(r"(?:方向|dir|direction)\s*[=:：]\s*(看多|看空|中性|偏多|偏空|bullish|bearish|neutral|long|short)", re.I)
_TGT_RE = re.compile(r"(?:目标价|tgt|target(?:\s*price)?)\s*[=:：]\s*" + _NUM, re.I)
_SL_RE = re.compile(r"(?:止损价|stop(?:\s*loss)?|sl)\s*[=:：]\s*" + _NUM, re.I)
_CONF_RE = re.compile(r"(?:置信度|conf|confidence)\s*[=:：]\s*([0-9][0-9.]*)\s*(%|％)?", re.I)
_HZ_RE = re.compile(r"(?:周期|hz|horizon)\s*[=:：]\s*([0-9]+)\s*(?:天|日|days?|d)?", re.I)


def _num(s):
    try:
        return float(s.replace(",", ""))
    except (ValueError, AttributeError):
        return None


ENTRY_RE = re.compile(r"(?:入场价|entry(?:\s*price)?)\s*[=:：]\s*" + _NUM, re.I)
COND_RE = re.compile(r"(?:入场条件|cond)\s*[=:：]\s*(gte|lte|now|突破|回踩|涨到|跌到|现价)", re.I)
COND_MAP = {"突破": "gte", "涨到": "gte", "回踩": "lte", "跌到": "lte", "现价": "now",
            "gte": "gte", "lte": "lte", "now": "now"}
# 2026-10-30：立场行可选字段「对现有挂单=保留/改价/撤销/改现价」→ pending_action（只影响入场方案分组，不影响打分）
PEND_RE = re.compile(r"(?:对现有挂单|pending)\s*[=:：]\s*(保留|改价|撤销|撤单|改现价|现价|keep|reprice|cancel|now)", re.I)
PEND_MAP = {"保留": "keep", "改价": "reprice", "撤销": "cancel", "撤单": "cancel", "改现价": "now", "现价": "now",
            "keep": "keep", "reprice": "reprice", "cancel": "cancel", "now": "now"}


def _conf_value(m) -> float:
    v = float(m.group(1))
    if m.group(2) or v > 1:               # 写了 % 或数值 >1 按百分制理解（v1 会把 65 直接截成 1.0）
        v = v / 100 if v <= 100 else 1.0
    return max(0.0, min(1.0, v))


def _stance_fields(seg: str):
    """在单个【立场】段内逐字段提取；没有方向返回 None。_complete=置信度与周期齐全。"""
    d = _DIR_RE.search(seg)
    if not d:
        return None
    g = d.group(1)
    out = {"direction": DIR_MAP.get(g) or DIR_MAP.get(g.lower())}
    t, s = _TGT_RE.search(seg), _SL_RE.search(seg)
    out["target"] = _num(t.group(1)) if t else None
    out["stop"] = _num(s.group(1)) if s else None
    c = _CONF_RE.search(seg)
    out["confidence"] = _conf_value(c) if c else None
    h = _HZ_RE.search(seg)
    out["horizon_days"] = int(h.group(1)) if h else None
    em = ENTRY_RE.search(seg)             # 可选：建议入场价位与条件（v3 起条件是独立字段 cond=）
    if em:
        out["entry_px"] = _num(em.group(1))
    cm = COND_RE.search(seg)
    if cm:
        g = cm.group(1)
        out["entry_condition"] = COND_MAP.get(g) or COND_MAP.get(g.lower())
    pm = PEND_RE.search(seg)
    if pm:
        g = pm.group(1)
        out["pending_action"] = PEND_MAP.get(g) or PEND_MAP.get(g.lower())
    out["_complete"] = c is not None and h is not None
    return out


def parse_stance(text: str):
    text = text or ""
    idxs = [m.start() for m in re.finditer(r"【立场】|\[STANCE\]", text, re.I)]
    if not idxs:
        return None
    partial = None
    for i in range(len(idxs) - 1, -1, -1):   # 自最后一个立场段向前找（辩论多轮时最新观点优先）
        seg = text[idxs[i]: idxs[i + 1] if i + 1 < len(idxs) else len(text)].split("\n\n")[0]
        f = _stance_fields(seg)
        if not f:
            continue
        if f.pop("_complete"):
            f["source"] = "立场行"
            return f
        if partial is None:
            partial = f
    if partial is None:
        return None
    partial.pop("_complete", None)
    if partial["confidence"] is None:
        partial["confidence"] = 0.5
    partial["source"] = "立场行"
    partial["partial"] = True
    return partial


RATING = {"buy": ("bullish", 0.8), "overweight": ("bullish", 0.6), "hold": ("neutral", 0.5),
          "underweight": ("bearish", 0.6), "sell": ("bearish", 0.8)}
BAND = {"bullish": ("bullish", 1.0), "mildly bullish": ("bullish", 0.6), "neutral": ("neutral", 0.5),
        "mixed": ("neutral", 0.4), "mildly bearish": ("bearish", 0.6), "bearish": ("bearish", 1.0)}
CONF = {"low": 0.3, "medium": 0.5, "high": 0.7}


def _field(text, label):
    m = re.search(r"\*\*" + label + r"\*\*:?\s*\**:?\s*([^\n]+)", text or "", re.I)
    return m.group(1).strip().strip("*").strip() if m else None


def parse_structured(role: str, text: str):
    """研究经理/交易员/组合经理/情绪分析师在 TradingAgents 中是结构化输出，按其渲染格式读取。"""
    if role in ("research_manager", "portfolio_manager", "trader"):
        lab = {"research_manager": "Recommendation", "portfolio_manager": "Rating", "trader": "Action"}[role]
        v = (_field(text, lab) or "").lower()
        if v not in RATING:
            return None
        d, c = RATING[v]
        out = {"direction": d, "confidence": c, "target": None, "stop": None, "horizon_days": None, "source": "结构化输出"}
        if role == "portfolio_manager":
            out["target"] = _num((_field(text, "Price Target") or "").split()[0] if _field(text, "Price Target") else None)
            hz = re.search(r"(\d+)\s*(天|日|day)", _field(text, "Time Horizon") or "", re.I)
            out["horizon_days"] = int(hz.group(1)) if hz else None
        if role == "trader":
            out["entry"] = _num(_field(text, "Entry Price"))
            out["entry_px"] = out["entry"]
            out["stop"] = _num(_field(text, "Stop Loss"))
        return out
    if role == "sentiment":
        m = re.search(r"Overall Sentiment:\*\*\s*\*\*([A-Za-z ]+)\*\*", text or "")
        if not m or m.group(1).strip().lower() not in BAND:
            return None
        d, k = BAND[m.group(1).strip().lower()]
        cm = re.search(r"\*\*Confidence:\*\*\s*(\w+)", text)
        c = CONF.get(cm.group(1).lower(), 0.5) if cm else 0.5
        return {"direction": d, "confidence": round(c * k, 2), "target": None, "stop": None, "horizon_days": None,
                "source": "结构化输出"}
    return None


class RoleStance(BaseModel):
    role: str = Field(description="role key, exactly as given in the prompt")
    direction: Literal["bullish", "bearish", "neutral"] = Field(description="view on future price: bullish = advise long, bearish = advise short, neutral = no trade")
    target: float | None = Field(default=None, description="target price (absolute USDT level); omit if none")
    stop: float | None = Field(default=None, description="stop-loss / invalidation level; omit if none")
    confidence: float = Field(description="0..1")
    horizon_days: int = Field(default=5, description="view horizon in days")
    entry_px: float | None = Field(default=None, description="suggested entry trigger price (USDT); omit if not stated")
    entry_condition: Literal["gte", "lte", "now"] | None = Field(default=None, description="gte = open when price rises to >= entry, lte = falls to <= entry, now = open at market")
    pending_action: Literal["keep", "reprice", "cancel", "now"] | None = Field(
        default=None, description="attitude to an existing same-direction pending order: keep / reprice at entry_px / cancel / open now at market; omit if not mentioned")
    reason: str = Field(description="one-line reason in Chinese (<=40 chars)")


class StanceList(BaseModel):
    stances: list[RoleStance]


# L3：立场状态。failed/absent/data_missing 是"没有有效意见"，既不进共识分子也不进分母
INVALID_STATUS = ("failed", "absent", "data_missing")
_SRC_STATUS = (("抽取失败", "failed"), ("无发言", "absent"), ("LLM抽取", "extracted"), ("结构化", "structured"),
               ("立场行", "parsed"), ("交易经理", "structured"))


def stance_status(s: dict) -> str:
    """返回立场状态：parsed/structured/extracted/neutral_active/failed/absent/data_missing。
    已显式写了 status 的（例如 L4 数据缺失）以显式为准；中性且置信度>0 记 neutral_active（有效弃权票）。"""
    if s.get("status") in INVALID_STATUS:
        return s["status"]
    src = str(s.get("source") or "")
    for k, v in _SRC_STATUS:
        if src.startswith(k) and v in ("failed", "absent"):
            return v
    if s.get("direction") == "neutral" and float(s.get("confidence") or 0) > 0:
        return "neutral_active"
    for k, v in _SRC_STATUS:
        if k in src:
            return v
    return s.get("status") or "parsed"


def normalize(s: dict) -> dict:
    """B1：所有加权角色同一规则，动作（方向）标签为准。中性/平仓立场不是多空票，入场与价位字段失效，冲突记录在 field_conflict。"""
    if s.get("direction") not in ("neutral", "close"):
        return s
    keys = [k for k in ("entry_px", "entry", "entry_condition", "pending_action", "target", "stop") if s.get(k) not in (None, "")]
    if not keys:
        return s
    s = dict(s)
    prev = s.get("field_conflict")
    for k in keys:
        s[k] = None
    keys = [k for k in keys if k != "pending_action"]
    if not keys:
        return s
    s["field_conflict"] = ((prev + "；") if prev else "") + f"方向={s['direction']}，字段 {','.join(keys)} 已忽略（以动作标签为准）"
    return s


def cross_check(role: str, prose: dict | None, structured: dict | None) -> dict | None:
    """P3（2026-11-15 用户修订口径）：自由文本立场是主源，vendor 结构化输出只做交叉校验。
    LLM 自由表达、代码负责抽取与核对；任何情况都不拒绝、不重问，只把冲突落成标记
    （cross_check 字段，随决策记录进日报「立场解析健康」）：
    - 只有结构化（立场行抽不出）：用结构化，记 prose_missing——意见不丢。
    - 两者都有且方向/价位冲突：立场行完整（非 partial）时以立场行为准（那是分析文本
      自己的结论）；立场行残缺（抽取含糊）时以结构化为准并补齐缺失价位字段。
    - 置信度差异过大只记不改（结构化侧是评级映射的粗粒度值）。一致则静默。"""
    if structured is None:
        return prose
    if prose is None:
        out = dict(structured)
        out["cross_check"] = "prose_missing: structured output used"
        return out
    s = dict(prose)
    notes = []
    ambiguous = bool(s.get("partial"))
    if structured.get("direction") and structured["direction"] != s.get("direction"):
        if ambiguous:
            notes.append(f"direction: prose {s.get('direction')} (partial) vs structured "
                         f"{structured['direction']} -> structured")
            s["direction"] = structured["direction"]
        else:
            notes.append(f"direction: prose {s.get('direction')} vs structured "
                         f"{structured['direction']} -> prose")
    for key in ("target", "stop", "entry_px", "entry"):
        pv, sv = s.get(key), structured.get(key)
        if pv is not None and sv is not None:
            if pv and abs(float(sv) - float(pv)) / max(abs(float(pv)), 1e-9) > 0.005:
                if ambiguous:
                    notes.append(f"{key}: prose {pv} (partial) vs structured {sv} -> structured")
                    s[key] = sv
                else:
                    notes.append(f"{key}: prose {pv} vs structured {sv} -> prose")
        elif pv is None and sv is not None and ambiguous:
            s[key] = sv
            notes.append(f"{key}: filled from structured (prose partial)")
    if (structured.get("confidence") is not None and s.get("confidence") is not None
            and abs(float(structured["confidence"]) - float(s["confidence"])) > 0.35):
        notes.append(f"confidence: prose {s['confidence']} vs structured "
                     f"{structured['confidence']} (kept prose)")
    if notes:
        s["cross_check"] = "; ".join(notes)
        if any("-> structured" in n or "filled from structured" in n for n in notes):
            s["source"] = "结构化+立场行"
    return s


def extract(fs: dict, cfg, tracker=None) -> dict:
    texts = role_texts(fs)
    out = {}
    for r in ROLE_KEYS:
        s = cross_check(r, parse_stance(texts[r]), parse_structured(r, texts[r]))
        if s:
            out[r] = s
    missing = [r for r in ROLE_KEYS if r not in out and texts[r].strip()]
    if missing:
        model = dllm.make_llm(cfg, "quick", tracker, role="extractor").with_structured_output(StanceList)
        body = "\n\n".join(f"### role={r} ({ROLE_CN[r]})\n{texts[r][-6000:]}" for r in missing)
        prompt = ("Below are statements by roles of a crypto trading desk about " + fs.get("company_of_interest", "") + ". "
                  "Extract each role's view (direction, target, stop, confidence, horizon, suggested entry + condition, "
                  "attitude to existing pending orders, one-line reason). Use only the text; do not guess; omit fields the "
                  "text does not state. Output only these roles: " + ", ".join(missing) + "\n\n" + body)
        try:
            res = model.invoke(prompt)
            for s in res.stances:
                if s.role in missing:
                    out[s.role] = {"direction": s.direction, "target": s.target, "stop": s.stop,
                                   "confidence": max(0.0, min(1.0, s.confidence)), "horizon_days": s.horizon_days,
                                   "entry_px": s.entry_px, "entry_condition": s.entry_condition, "pending_action": s.pending_action,
                                   "reason": s.reason, "source": "LLM抽取"}
        except Exception as e:  # 抽取失败不阻断
            for r in missing:
                out[r] = {"direction": "neutral", "confidence": 0.0, "source": f"抽取失败:{type(e).__name__}"}
    for r in ROLE_KEYS:
        out.setdefault(r, {"direction": "neutral", "confidence": 0.0, "source": "无发言"})
        out[r] = normalize(out[r])
        out[r]["status"] = stance_status(out[r])
    # L4：输入数据缺失的角色记 data_missing（不进共识、不打分）；fs 里没有标记（旧路径）时不动
    dq = (cfg.get("data_quality") or {}) if isinstance(cfg, dict) else {}
    if dq.get("sentiment_requires_x_feed", True) and fs.get("_x_feed") is False:
        out["sentiment"]["status"] = "data_missing"; out["sentiment"]["data_note"] = "Threads 讨论摘录缺失"
    if fs.get("_flows_missing"):
        out["flows"]["status"] = "data_missing"; out["flows"]["data_note"] = "资金流数据过半获取失败"
    # P1：分析师工具结果被上限截断 → 落到对应角色的 data_note（不改状态：截断≠数据缺失，
    # 分析师在结果里也看得到英文内联注记；这里保证它进决策记录的数据缺口口径，绝不静默）。
    truncs = fs.get("_tool_truncations") or []
    if truncs:
        tool_role = {"get_stock_data": "market", "get_indicators": "market", "run_backtest": "market",
                     "get_verified_market_snapshot": "market", "get_news": "news",
                     "get_prediction_markets": "news", "get_macro_indicators": "news"}
        by_role: dict[str, list] = {}
        for t in truncs:
            by_role.setdefault(tool_role.get(t.get("tool"), "market"), []).append(t)
        for r, ts in by_role.items():
            note = "; ".join(f"tool output truncated: {t.get('tool')} {t.get('chars')}->{t.get('cap')} chars"
                             for t in ts)
            out[r]["data_note"] = ((out[r].get("data_note") + "; ") if out[r].get("data_note") else "") + note
        out[r]["horizon_days"] = out[r].get("horizon_days") or cfg["scoring"]["horizon_days"]
        if not out[r].get("reason"):
            fld = None
            for lab in ("Executive Summary", "Rationale", "Reasoning"):
                fld = fld or _field(texts[r], lab)
            if r == "sentiment":
                m = re.search(r"\n\n([^#*|\n][^\n]{20,})", texts[r] or "")
                fld = m.group(1) if m else None
            out[r]["reason"] = (fld or "")[:80] or _first_line(texts[r])
    return out


def _first_line(t: str) -> str:
    t = re.sub(r"[#*>`|]", "", t or "").strip()
    return t.split("\n")[0][:60] if t else ""


# ---- 解析健康（2026-11-02）：降级不再静默 -------------------------------------
# “降级”= 加权角色的立场没有走精确解析：LLM 粗抽、残缺立场（partial）或抽取失败。
# 缺席/数据缺失是参与与数据问题（另有口径上报），不算解析降级。

def degraded(s: dict) -> bool:
    if not s:
        return False
    if s.get("partial") or s.get("status") == "failed":
        return True
    return str(s.get("source") or "").startswith(("LLM抽取", "抽取失败"))


def _tier(s: dict) -> str:
    src = str(s.get("source") or "")
    if s.get("status") == "failed" or src.startswith("抽取失败"):
        return "failed"
    if src.startswith("LLM抽取"):
        return "extracted"
    if "结构化" in src:
        return "structured"
    if src.startswith("立场行"):
        return "parsed"
    return "other"


def health(decisions, weights) -> dict:
    """统计一组决策记录里加权角色（不含交易经理：执行角色，缺席另有 tm_absent_reason）的解析途径与降级清单。"""
    tiers = {"parsed": 0, "structured": 0, "extracted": 0, "failed": 0, "other": 0}
    bad = []
    conflicts = []
    roles = [r for r in (weights or {}) if r != "trade_manager"]
    for d in decisions or []:
        st = d.get("stances") or {}
        for r in roles:
            s = st.get(r)
            if not s:
                continue
            if s.get("status") in ("absent", "data_missing") or str(s.get("source") or "") == "无发言":
                tiers["other"] += 1
                continue
            tiers[_tier(s)] += 1
            if degraded(s):
                bad.append({"inst_id": d.get("inst_id"), "role": r, "source": s.get("source"),
                            "status": s.get("status"), "partial": bool(s.get("partial"))})
            if s.get("cross_check"):   # P3：立场行×结构化交叉校验的冲突/补位（日报可见）
                conflicts.append({"inst_id": d.get("inst_id"), "role": r, "cross_check": s["cross_check"]})
    return {"tiers": tiers, "degraded": bad, "conflicts": conflicts, "total": sum(tiers.values())}
