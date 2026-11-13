"""兜底市价补单前的交易经理判定（2026-11-11 用户指令）。

背景：条件入场单触发后限价子单未成交时，entry_limit 原逻辑在超时撤单后无条件用市价补剩余。
2026-11-11 ETH 空单即如此出事：触发价 2075，价格急跌，约 3 分钟后市价兜底成交在 2055.74
（偏离计划 19.26），仓位实际 RR 由计划 1.76 塌到 ≈1.007。用户决定：兜底机制本身保留，
但补单前插入一次判定——以「交易经理」身份（判断自己挂出的这张入场单在当前价位是否还值得进）、
用快速模型（默认 deepseek-flash，与 llm.quick_model 同档，绝不用 TM 席位的 opus 推理档）判断
「触发后这段时间过去了，这个点位开是否合理」。

语义：
- 只出现在市价兜底路径：限价正常成交不调用；场次内现价开仓（open_now，决策时现判）不调用。
- verdict=proceed → 原兜底流程不变；verdict=abandon → 不发市价单，本次入场按未成交收尾
  （已成交部分与护栏 blocked 路径同样处理：照常 _protect_total 挂保护），事件 fallback_abandoned 落盘。
- 判定调用失败/不可解析（call_judge 返回 None）→ 按未批准处理（abandon），事件 fallback_judge_failed
  + fallback_abandoned 落盘。本层最多重试 1 次（共 2 次尝试）；watch/ws 进程内 BaseChatModel.invoke
  未装 llm_retry 全局退避（只在 run_daily/rediscuss 等子进程装），不会被退避拉长巡检周期。
- 开关 execution.entry_limit.fallback_judge_enabled=false → 完全恢复原无条件兜底行为。
判定结论由 entry_limit 落进 entry 状态（fallback_judge 字段）后再执行，进程重启/重入不重复判定。
"""
from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, Field

from desk import okx_demo as X
from desk import settings
from desk import llm as dllm
from desk.sizing import rr_ratio


def judge_cfg(cfg) -> dict:
    """判定配置（execution.entry_limit 段，经 demo_exec.ecfg 合并默认值）。"""
    from desk import demo_exec as DX
    el = DX.ecfg(cfg)["entry_limit"]
    return {"enabled": bool(el.get("fallback_judge_enabled", True)),
            "model": str(el.get("fallback_judge_model") or "deepseek-flash"),
            "provider": str(el.get("fallback_judge_provider") or "deepseek")}


def _num(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


# ------------------------------------------------------------------ 事实包
def _atr14_daily(inst) -> float | None:
    """日线 ATR14（与 manager.market_ctx 同口径：TR 的 14 日均值，60 根已收盘日线）；
    取不到（数据层异常/离线）返回 None，判定提示词改用绝对值与百分比表述。"""
    try:
        from desk import okx_data
        d = okx_data.candles(inst, "1Dutc", 60)
        c, h, l = d.Close, d.High, d.Low
        tr = (h - l).combine((h - c.shift()).abs(), max).combine((l - c.shift()).abs(), max)
        v = float(tr.tail(14).mean())
        return v if v > 0 else None
    except Exception:
        return None


def _spot(inst) -> float | None:
    try:
        v = float(X.get("/api/v5/market/ticker", instId=inst)["data"][0]["last"])
        return v if v > 0 else None
    except Exception:
        return None


def _exposure(e) -> dict:
    """当前持仓快照（总持仓数、本币现有合约数——含本单已成交部分）；读取失败记 None，不阻断判定。"""
    try:
        from desk import demo_exec as DX
        rows = DX.demo_positions()
        same = sum(abs(_num(p.get("pos"))) for p in rows
                   if p.get("instId") == e["inst"] and p.get("posSide") in (e.get("pos_side", "net"),))
        return {"positions_count": len(rows), "same_coin_contracts": same}
    except Exception:
        return {"positions_count": None, "same_coin_contracts": None}


def build_facts(cfg, e, *, filled, remain, spot, now_ms) -> dict:
    """组装判定事实包（全部为可 JSON 序列化的数值/字符串）。偏离口径：worse>0 表示现价成交比
    计划入场更差（做多=现价高于触发价，做空=现价低于触发价），单位与 dev_atr 同步给出。"""
    side = e["side"]
    sg = 1 if side == "long" else -1
    trig, limit = _num(e.get("trigger_px")), _num(e.get("order_px"))
    sl, tp = e.get("stop_loss"), e.get("take_profit")
    atr = _atr14_daily(e["inst"])
    dev = spot - trig
    worse = sg * dev
    facts = {"inst": e["inst"], "side": side, "pos_side": e.get("pos_side", "net"),
             "add": bool(e.get("add")), "decision_id": e.get("decision_id"),
             "trigger_px": trig, "limit_px": limit,
             "stop_loss": _num(sl) if sl is not None else None,
             "take_profit": _num(tp) if tp is not None else None,
             "planned_sz": _num(e.get("sz")), "filled_sz": float(filled), "remain_sz": float(remain),
             "planned_rr": rr_ratio(side, trig, sl, tp),
             "spot": spot, "decision_spot": None,   # 入场登记未记录决策时现价（见模块头注），以触发价为计划基准
             "elapsed_secs": round(max(0.0, (now_ms - int(e.get("triggered_ms") or now_ms)) / 1000.0), 1),
             "dev_from_trigger": round(dev, 6), "dev_pct": round(dev / trig * 100, 3) if trig else None,
             "worse_amount": round(worse, 6),
             "atr14": atr, "dev_atr": round(worse / atr, 3) if atr else None,
             "effective_rr": rr_ratio(side, spot, sl, tp)}
    facts.update(_exposure(e))
    return facts


def judge_prompt(f: dict) -> str:
    side_cn = "做多（long）" if f["side"] == "long" else "做空（short）"
    rr_p = "（止损止盈不全，无法计算）" if f["planned_rr"] is None else f"{f['planned_rr']:.3f}"
    rr_e = "（止损止盈不全，无法计算）" if f["effective_rr"] is None else f"≈{f['effective_rr']:.3f}"
    atr_s = (f"偏离折合 {f['dev_atr']:+.2f}×ATR14（ATR14 {f['atr14']:.6g}）" if f["dev_atr"] is not None
             else "ATR14 数据不足，仅以绝对值与百分比判断偏离")
    pos_s = ("数据不足" if f["positions_count"] is None
             else f"当前总持仓 {f['positions_count']} 个；本币现有持仓 {f['same_coin_contracts']:g} 张（含本单已成交部分）")
    return "\n".join([
        "你是交易经理。这张条件入场单出自你（交易台）在本日决策环节的判断，现在它触发后限价子单未成交，"
        "执行层准备用市价补单。补单前由你亲自判断：在当前价位入场，这笔交易是否仍然成立。",
        "",
        "## 原计划",
        f"- 标的/方向：{f['inst']} {side_cn}" + ("（条件加仓单）" if f["add"] else ""),
        f"- 触发价 {f['trigger_px']:g}；限价子单价 {f['limit_px']:g}；止损 {f['stop_loss']}；止盈 {f['take_profit']}",
        f"- 计划张数 {f['planned_sz']:g}（已成交 {f['filled_sz']:g}，待补 {f['remain_sz']:g}）",
        f"- 按触发价计的计划盈亏比 RR = {rr_p}",
        "",
        "## 当前状况",
        f"- 触发已过 {f['elapsed_secs']:g} 秒；现价 {f['spot']:g}，较触发价 {f['dev_from_trigger']:+g}（{f['dev_pct']:+.2f}%），{atr_s}",
        f"- 若现在按现价市价成交，实际盈亏比 RR {rr_e}",
        f"- 持仓快照：{pos_s}",
        "",
        "## 判断口径",
        "- 只判断一件事：以当前价位入场是否仍服务于原决策。价格已跑得太远、RR 明显塌陷、走势已透支 → abandon；"
        "仅小幅漂移、原逻辑未变 → proceed。",
        "- 不许提出新价格、新张数、新止损止盈；只有 proceed / abandon 两个结论，reason 用一句话说清依据。",
        "只输出结构化结果，不要输出其他文字。"])


# ------------------------------------------------------------------ LLM 判定
class FallbackJudgment(BaseModel):
    verdict: Literal["proceed", "abandon"] = Field(description="proceed=按现价市价补单入场仍合理；abandon=放弃本次入场，不追价")
    reason: str = Field(description="一句话中文理由（≤80 字）")


def normalize_judgment(obj) -> dict | None:
    """结构化输出（FallbackJudgment 实例或 dict）→ 归一化 dict；不合格返回 None（按判定失败处理）。"""
    try:
        if isinstance(obj, FallbackJudgment):
            d = obj.model_dump()
        elif isinstance(obj, dict):
            d = FallbackJudgment(**obj).model_dump()
        else:
            return None
    except Exception:
        return None
    if d["verdict"] not in ("proceed", "abandon"):
        return None
    return {"verdict": d["verdict"], "reason": str(d.get("reason") or "")[:200]}


def _judge_model(cfg, tracker):
    """快速档模型：默认走 llm.make_llm 的 quick 通道（= llm.quick_model，当前 deepseek-flash，
    与 market/flows 等快速席位同档同管道，含 TA_DESK_MOCK_LLM 测试替身）；配置改指其它
    provider/model 时按 llm 段端点直建。不套交易经理手册——这是限时执行判定，与立场抽取同例。"""
    jc = judge_cfg(cfg)
    L = cfg.get("llm") or {}
    if jc["model"] == L.get("quick_model") and jc["provider"] == L.get("provider"):
        return dllm.make_llm(cfg, "quick", tracker).with_structured_output(FallbackJudgment)
    if os.environ.get("TA_DESK_MOCK_LLM") == "1":
        return dllm.FakeChatModel(tier="quick", callbacks=[tracker] if tracker else None).with_structured_output(FallbackJudgment)
    from tradingagents.llm_clients import create_llm_client
    dllm.prepare_env(cfg)
    kw = {"callbacks": [tracker]} if tracker else {}
    return create_llm_client(jc["provider"], jc["model"], settings.llm_base_url(cfg), **kw).get_llm().with_structured_output(FallbackJudgment)


def call_judge(cfg, prompt: str, inst: str) -> dict | None:
    """调用判定：本层最多 2 次尝试（初次 + 重试 1 次），无退避睡眠；构建/调用/解析任一失败 → None。
    用量经独立 UsageTracker 落 state/usage 日志，stage 标记为 <inst>/兜底判定。"""
    tracker = dllm.UsageTracker(cfg)
    try:
        model = _judge_model(cfg, tracker)
    except Exception:
        return None
    tok = dllm.STAGE.set(f"{inst}/兜底判定")
    try:
        for _ in range(2):
            try:
                out = model.invoke(prompt)
            except Exception:
                continue
            j = normalize_judgment(out)
            if j is not None:
                return j
        return None
    finally:
        dllm.STAGE.reset(tok)


def judge_fallback(cfg, e, *, filled, remain, now_ms) -> tuple[str, dict, dict | None]:
    """兜底前判定主入口。返回 (outcome, facts, judgment)：
    - "proceed"：判定批准（judgment 为判定原文）；
    - "abandon"：判定否决（judgment 为判定原文）；
    - "failed"：判定调用失败/不可解析（judgment=None），上层按未批准（abandon）处理并记 fallback_judge_failed；
    - "skip"：现价都取不到，无法组装事实包（judgment=None，facts 仅含 inst/side）——同一步的 _full_check
      刚用同一 ticker 源做过完整风控且失败即拦截，故此分支在生产中近乎不可达；上层按旧行为放行并记
      fallback_judge_skipped，避免为一次冗余取价失败误杀已过护栏的补单。"""
    spot = _spot(e["inst"])
    if spot is None:
        return "skip", {"inst": e["inst"], "side": e["side"], "why": "spot_unavailable"}, None
    facts = build_facts(cfg, e, filled=filled, remain=remain, spot=spot, now_ms=now_ms)
    try:
        j = call_judge(cfg, judge_prompt(facts), e["inst"])
    except Exception:   # call_judge 本应吞掉一切异常返回 None；此处再兜一层，保证判定环节绝不打断巡检
        j = None
    if j is None:
        return "failed", facts, None
    return j["verdict"], facts, j
