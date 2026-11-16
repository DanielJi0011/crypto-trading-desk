"""风险三人组报告摘要（P2，2026-11-15 LLM 数据流审查）。

激进/保守/中性三位风控辩手在 vendor 图里会拿到全部分析师报告原文（约 13.1K 字符/币）。
本模块由代码从报告集构建约 2.5K 字符的摘要（各角色方向+置信度、目标/止损/入场位、
市场 ATR 与关键位原文行、资金费率与持仓量、每份报告一句话摘要），经 roles.install
的节点包裹替换三份报告（vendor 源码不改）。

完整性校验（2026-11-15 用户硬约束）：摘要由台子自己的代码构建，所以必须自检——
原文里解析得到的每个必填字段都必须出现在摘要里；某份报告非空却解析不出方向+置信度、
或市场报告里有 ATR 却没进摘要、或资金面数据里有费率却没进摘要 → 本次调用回退原文
全集，并记 digest_incomplete_fallback 事件（进程内 EVENTS + state/llm_flow_events.jsonl
+ warning 日志）。摘要路径绝不静默发出不完整的摘要。原文自身就没有的字段（某角色
没给目标价之类）不算缺失——那是既有的数据缺口口径，不由摘要背锅。
"""
from __future__ import annotations

import logging
import re

from desk import settings, stances

log = logging.getLogger("desk.digest")

EVENTS: list[dict] = []          # 本进程的摘要回退事件（测试与运行汇总可读）
EVENT_FILE = "llm_flow_events.jsonl"
FALLBACK_EVENT = "digest_incomplete_fallback"

_ONE_LINER_MAX = 220
_LEVEL_LINE_MAX = 300
_LEVEL_WORDS = ("支撑", "阻力", "support", "resistance", "关键位", "key level")


def _fmt(v) -> str:
    return "none" if v is None else f"{float(v):.6g}"


def role_view(role: str, text: str) -> dict | None:
    """与 stances.extract 同口径取一个角色的观点：立场行解析为主、结构化解析补方向。"""
    s = stances.parse_stance(text)
    st = stances.parse_structured(role, text)
    if st and s:
        s = {**s, **{k: v for k, v in st.items() if v is not None and k == "direction"}}
        return s
    return s or st


_ATR_RE = re.compile(r"ATR\s*14[^0-9]{0,16}([0-9][0-9,]*\.?[0-9]*)|ATR[^0-9]{0,16}([0-9][0-9,]*\.?[0-9]*)", re.I)


def _atr_of(market_text: str):
    m = _ATR_RE.search(market_text or "")
    if not m:
        return None
    return stances._num(m.group(1) or m.group(2))


def _level_lines(text: str) -> list[str]:
    out = []
    for ln in (text or "").splitlines():
        t = ln.strip().lstrip("#-* ").strip()
        if t and any(w in t.lower() for w in _LEVEL_WORDS) and any(ch.isdigit() for ch in t):
            out.append(t[:_LEVEL_LINE_MAX])
        if len(out) >= 3:
            break
    return out


def _one_liner(text: str) -> str:
    for ln in (text or "").splitlines():
        t = ln.strip().lstrip("#-* ").strip()
        if len(t) < 12:
            continue
        if "【立场】" in t or "[STANCE]" in t.upper():
            continue
        if t.startswith(("Overall Sentiment", "**Overall Sentiment", "Confidence:", "**Confidence")):
            continue
        return t[:_ONE_LINER_MAX]
    return ""


_FUNDING_RE = re.compile(r"funding_now \(8h\):\s*([+-]?[0-9.]+)%")
_OI_RE = re.compile(r"oi_usd\):\s*([0-9.]+) billion")


def build_digest(reports: dict, flows_report: str = "", flows_data: str = "") -> dict:
    """reports: {"market"/"sentiment"/"news"/"fundamentals": 图状态原文}。
    返回 {"slots": {报告槽位: 摘要段}, "text": 全部摘要拼接, "missing": [缺失字段名]}。
    missing 非空 = 摘要不完整，调用方必须回退原文全集。"""
    missing: list[str] = []
    sections: dict[str, str] = {}
    views: dict[str, dict | None] = {}
    for role in ("market", "sentiment", "news"):
        text = reports.get(role) or ""
        if not text.strip():
            sections[role] = f"[{role}] no report this run (data gap, already accounted)"
            views[role] = None
            continue
        v = role_view(role, text)
        if not v or v.get("direction") is None or v.get("confidence") is None:
            missing.append(f"{role}.stance")
            views[role] = None
            continue
        views[role] = v

    mtext = reports.get("market") or ""
    atr = _atr_of(mtext)
    if views.get("market"):
        v = views["market"]
        head = (f"dir={v['direction']} conf={float(v['confidence']):.2f} tgt={_fmt(v.get('target'))} "
                f"stop={_fmt(v.get('stop'))} entry={_fmt(v.get('entry_px'))}"
                + (f" ({v['entry_condition']})" if v.get("entry_condition") else "")
                + f" hz={v.get('horizon_days') or '?'}d")
        if atr is None and mtext.strip():
            missing.append("market.atr")
        parts = [f"[market] {head}", f"ATR14: {_fmt(atr)}"]
        parts += [f"levels: {ln}" for ln in _level_lines(mtext)]
        sections["market"] = "\n".join(parts)
    for role in ("sentiment", "news"):
        if views.get(role):
            v = views[role]
            line = _one_liner(reports.get(role) or "")
            # 入场价与 market 槽位同口径进摘要：news 的条件式入场价（如“跌到 1,860”）
            # 是立场解析出的必填字段，摘要里不带它完整性自检会报 {role}.entry_px
            # 并回退全文——2026-11-16 早场 6 次回退全是这个缺口。
            entry = (f" entry={_fmt(v.get('entry_px'))}"
                     + (f" ({v['entry_condition']})" if v.get("entry_condition") else ""))
            sections[role] = (f"[{role}] dir={v['direction']} conf={float(v['confidence']):.2f} "
                              f"tgt={_fmt(v.get('target'))} stop={_fmt(v.get('stop'))}{entry}"
                              + (f" | {line}" if line else ""))
    ftext = reports.get("fundamentals") or ""
    if ftext.strip():
        v = role_view("fundamentals", ftext)
        if v and v.get("direction") is not None and v.get("confidence") is not None:
            sections["fundamentals"] = (f"[fundamentals] dir={v['direction']} "
                                        f"conf={float(v['confidence']):.2f} | {_one_liner(ftext)}")
        else:
            sections["fundamentals"] = f"[fundamentals] {_one_liner(ftext) or ftext.strip()[:_ONE_LINER_MAX]}"

    flow_bits = []
    fm = _FUNDING_RE.search(flows_data or "")
    if fm:
        flow_bits.append(f"funding_now={fm.group(1)}%")
    elif (flows_data or "").strip():
        missing.append("flows.funding")
    om = _OI_RE.search(flows_data or "")
    if om:
        flow_bits.append(f"oi=${om.group(1)}B")
    fv = role_view("flows", flows_report) if (flows_report or "").strip() else None
    if fv and fv.get("direction") is not None and fv.get("confidence") is not None:
        flow_bits.append(f"flows dir={fv['direction']} conf={float(fv['confidence']):.2f}")
    fline = _one_liner(flows_report or "")
    if fline:
        flow_bits.append(fline)
    flows_section = "[flows] " + ("; ".join(flow_bits) if flow_bits else "no flows data this run")

    header = ("[RISK DIGEST — code-built from the analyst reports; every value below is taken "
              "verbatim from them, full texts omitted]")
    slots = {"market": header + "\n" + sections.get("market", "") + "\n" + flows_section,
             "sentiment": sections.get("sentiment", ""),
             "news": sections.get("news", "")}
    if "fundamentals" in sections:
        slots["fundamentals"] = sections["fundamentals"]
    text = "\n".join(slots[k] for k in ("market", "sentiment", "news", "fundamentals") if k in slots)

    # ---- 完整性自检：解析得到的必填值必须逐字出现在摘要里
    for role in ("market", "sentiment", "news"):
        v = views.get(role)
        if not v:
            continue
        if v.get("direction") and v["direction"] not in text:
            missing.append(f"{role}.direction")
        for key in ("target", "stop", "entry_px"):
            val = v.get(key)
            if val is not None and _fmt(val) not in text:
                missing.append(f"{role}.{key}")
    if atr is not None and _fmt(atr) not in text:
        missing.append("market.atr")
    if fm and fm.group(1) not in text:
        missing.append("flows.funding")
    return {"slots": slots, "text": text, "missing": missing}


def run_with_digest(node, state):
    """风险三人组节点包裹：先试摘要；构建失败或完整性不过关 → 用原文状态调原节点并留痕。"""
    reports = {"market": state.get("market_report") or "", "sentiment": state.get("sentiment_report") or "",
               "news": state.get("news_report") or "", "fundamentals": state.get("fundamentals_report") or ""}
    try:
        from desk import roles
        flows_brief = roles.BRIEFS.get("trader") or roles.BRIEFS.get("bull") or ""
        flows_report = (flows_brief.split("\n", 1)[1]
                        if flows_brief.startswith("##") and "\n" in flows_brief else flows_brief)
        built = build_digest(reports, flows_report, roles.BRIEFS.get("flows_data") or "")
    except Exception as e:
        log.warning("digest build failed (%s), using full reports", type(e).__name__)
        return node(state)
    if built["missing"]:
        rec = {"event": FALLBACK_EVENT, "missing": built["missing"], "time": settings.now_str()}
        EVENTS.append(rec)
        log.warning("%s: %s", FALLBACK_EVENT, ", ".join(built["missing"]))
        try:
            settings.append_jsonl(settings.STATE / EVENT_FILE, rec)
        except Exception:
            pass
        return node(state)
    new_state = dict(state)
    new_state["market_report"] = built["slots"]["market"]
    new_state["sentiment_report"] = built["slots"]["sentiment"]
    new_state["news_report"] = built["slots"]["news"]
    if "fundamentals" in built["slots"]:
        new_state["fundamentals_report"] = built["slots"]["fundamentals"]
    return node(new_state)
