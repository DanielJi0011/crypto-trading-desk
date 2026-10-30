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
           "bullish": "bullish", "bearish": "bearish", "neutral": "neutral"}
_NUM = r"([0-9][0-9,]*\.?[0-9]*|无|N/?A|none)"
STANCE_RE = re.compile(r"【立场】\s*方向\s*[=:：]\s*(看多|看空|中性|偏多|偏空)\s*[；;,，]\s*目标价\s*[=:：]\s*" + _NUM +
                       r"\s*[；;,，]\s*止损价\s*[=:：]\s*" + _NUM + r"\s*[；;,，]\s*置信度\s*[=:：]\s*([0-9.]+)\s*[；;,，]\s*周期\s*[=:：]\s*([0-9]+)",
                       re.I)


def _num(s):
    try:
        return float(s.replace(",", ""))
    except (ValueError, AttributeError):
        return None


ENTRY_RE = re.compile(r"入场价\s*[=:：]\s*" + _NUM + r"(?:\s*[；;,，]\s*入场条件\s*[=:：]\s*(突破|回踩|涨到|跌到|现价))?", re.I)
COND_MAP = {"突破": "gte", "涨到": "gte", "回踩": "lte", "跌到": "lte", "现价": "now"}
# 2026-10-30：立场行可选字段「对现有挂单=保留/改价/撤销/改现价」→ pending_action（只影响入场方案分组，不影响打分）
PEND_RE = re.compile(r"对现有挂单\s*[=:：]\s*(保留|改价|撤销|撤单|改现价|现价)")
PEND_MAP = {"保留": "keep", "改价": "reprice", "撤销": "cancel", "撤单": "cancel", "改现价": "now", "现价": "now"}


def parse_stance(text: str):
    ms = STANCE_RE.findall(text or "")
    if not ms:
        return None
    d, tgt, sl, conf, hz = ms[-1]          # 取最后一次（辩论多轮时为最新观点）
    out = {"direction": DIR_MAP[d], "target": _num(tgt), "stop": _num(sl),
           "confidence": max(0.0, min(1.0, float(conf))), "horizon_days": int(hz), "source": "立场行"}
    line = (text or "")[(text or "").rfind("【立场】"):].split("\n")[0]
    em = ENTRY_RE.search(line)            # 可选：建议入场价位与条件
    if em:
        out["entry_px"] = _num(em.group(1))
        out["entry_condition"] = COND_MAP.get(em.group(2) or "", None)
    pm = PEND_RE.search(line)
    if pm:
        out["pending_action"] = PEND_MAP[pm.group(1)]
    return out


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
    role: str = Field(description="角色键名，取值见提示")
    direction: Literal["bullish", "bearish", "neutral"] = Field(description="对未来价格方向的观点（bullish=建议做多，bearish=建议做空，neutral=不交易）")
    target: float | None = Field(default=None, description="目标价（USDT 绝对价位），没有就省略")
    stop: float | None = Field(default=None, description="止损价/失效价位，没有就省略")
    confidence: float = Field(description="0~1")
    horizon_days: int = Field(default=5, description="观点周期（天）")
    entry_px: float | None = Field(default=None, description="建议的开仓触发价位（USDT），原文没写就省略")
    entry_condition: Literal["gte", "lte", "now"] | None = Field(default=None, description="gte=价格涨到≥该价时开仓，lte=跌到≤该价时开仓，now=现价开仓")
    pending_action: Literal["keep", "reprice", "cancel", "now"] | None = Field(
        default=None, description="对现有同向条件挂单的态度：keep=保留，reprice=改价（以 entry_px 为准），cancel=撤销，now=改现价；原文没提就省略")
    reason: str = Field(description="一句话中文理由（≤40字）")


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


def extract(fs: dict, cfg, tracker=None) -> dict:
    texts = role_texts(fs)
    out = {}
    for r in ROLE_KEYS:
        s = parse_stance(texts[r])
        st = parse_structured(r, texts[r])
        if st and s:   # 结构化方向为准，立场行补充价位
            s = {**s, **{k: v for k, v in st.items() if v is not None and k in ("direction",)}, "source": "结构化+立场行"}
        s = s or st
        if s:
            out[r] = s
    missing = [r for r in ROLE_KEYS if r not in out and texts[r].strip()]
    if missing:
        model = dllm.make_llm(cfg, "quick", tracker, role="extractor").with_structured_output(StanceList)
        body = "\n\n".join(f"### 角色键名={r}（{ROLE_CN[r]}）\n{texts[r][-6000:]}" for r in missing)
        prompt = ("下面是一个加密货币交易团队中各角色关于 " + fs.get("company_of_interest", "") + " 的发言。"
                  "请逐个角色提取其观点（方向、目标价、止损价、置信度、周期、建议入场价与条件、对现有挂单的态度、一句话理由）。"
                  "只根据原文，不要推测；原文没写价位就省略该字段。只输出这些角色：" + ", ".join(missing) + "\n\n" + body)
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
        out["sentiment"]["status"] = "data_missing"; out["sentiment"]["data_note"] = "X 讨论摘录缺失"
    if fs.get("_flows_missing"):
        out["flows"]["status"] = "data_missing"; out["flows"]["data_note"] = "资金流数据过半获取失败"
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
