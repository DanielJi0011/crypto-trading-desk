"""持有期到期由交易经理判断"延期或平仓"（2026-10-29 用户决定）。由 desk/demo_watch.py 到期分支调用。

规则（代码强制，交易经理只给建议）：
- 配置 expiry_extend: {enabled, max_extend_days, max_extensions, pre_window_minutes, retry_delays}；缺 enabled 视为关闭=旧行为。
- 只有 止损未触发（仓位仍在）且 交易经理选 extend 且 最新共识方向与持仓同向（|S|>manager.consensus_threshold）才延期；
  延期天数 = min(extend_days, max_extend_days - 已延期天数)；总持有期 ≤ horizon_days + max_extend_days。
- 达上限 / 交易经理调用失败（重试 3 次仍失败，含跨轮超时 3 次）/ 输出不合规 → 旧行为：到期平仓。HALT 由调用方处理（不平仓、也不调用）。
- 延期：更新登记 deadline，记 extensions、extended_days、orig_deadline；不改止损止盈。
- 结论先写进登记（expiry_verdict，按当期 deadline 键），重启后不重复调用交易经理，通知也只发一次。
不调用任何下单接口；平仓由调用方经 demo_exec.close 执行。"""
from __future__ import annotations

import datetime as _dt
from typing import Literal

from pydantic import BaseModel, Field

from desk import settings

FMT = "%Y-%m-%d %H:%M:%S%z"
DEFAULTS = {"enabled": False, "mode": "full_discussion", "max_extend_days": 3, "max_extensions": 2, "pre_window_minutes": 5,
            "retry_delays": [2, 5, 10], "max_round_attempts": 3, "discussion_timeout_minutes": 45}
MODES = ("full_discussion", "simple", "off")


def mode(cfg) -> str:
    """off=旧行为到期直接平仓；simple=62648d3 交易经理简化问答；full_discussion=全员讨论（默认，desk/expiry_discuss.py）。enabled=false 等同 off。"""
    c = ecfg(cfg)
    if not c["enabled"]:
        return "off"
    m = str(c.get("mode") or "full_discussion").strip().lower()
    return m if m in MODES else "full_discussion"


class ExpiryDecision(BaseModel):
    action: Literal["close", "extend"] = Field(description="close=按期平仓；extend=延长持有期（仅当止损未触发且方向与最新共识一致）")
    extend_days: float | None = Field(default=None, description="action=extend 时填写延长天数（不超过题面给出的上限）")
    rationale: str = Field(default="", description="中文理由（≤100字）")


def ecfg(cfg) -> dict:
    d = dict(DEFAULTS)
    d.update((cfg or {}).get("expiry_extend") or {})
    return d


def due(cfg, m: dict, now) -> bool:
    """到期或到期前一个检查周期内（pre_window_minutes）。"""
    if not m.get("deadline"):
        return False
    dl = _dt.datetime.strptime(m["deadline"], FMT)
    win = _dt.timedelta(minutes=float(ecfg(cfg)["pre_window_minutes"])) if ecfg(cfg)["enabled"] else _dt.timedelta(0)
    return now >= dl - win


def latest_decision(inst: str) -> dict | None:
    from desk import manager
    recs = [r for r in settings.read_jsonl(manager.DECISIONS) if r.get("inst_id") == inst]
    return recs[-1] if recs else None


def consensus_agrees(cfg, side: str, S) -> bool:
    if S is None:
        return False
    th = float((cfg.get("manager") or {}).get("consensus_threshold", 0.1))
    return (float(S) > th) if side == "long" else (float(S) < -th)


def held_days(m: dict, now) -> float | None:
    for k in ("entry_filled_time", "opened"):
        try:
            return round((now - _dt.datetime.strptime(m[k], FMT)).total_seconds() / 86400, 2)
        except Exception:
            continue
    return None


def _prompt(cfg, inst, pos, m, now, dec, mk, left_days):
    side = pos.get("side")
    st = (dec or {}).get("stances") or {}
    from desk.roles import ROLE_CN
    lines = [f"你是加密货币交易台的交易经理。{inst}（OKX 永续，模拟盘）持仓持有期已到期或即将到期，请只回答：延期还是平仓。",
             f"持仓：{'空' if side == 'short' else '多'}单，入场均价 {pos.get('avg_px')}，现价 {pos.get('price')}，浮盈亏 {pos.get('upnl')} USDT，"
             f"止损 {pos.get('sl_px')}，止盈 {pos.get('tp_px')}，已持有 {held_days(m, now)} 天，原持有期 {m.get('horizon_days', 5)} 天，"
             f"当前到期 {m.get('deadline')}，已延期 {int(m.get('extensions') or 0)} 次。",
             f"行情：{mk}" if mk else "行情：未取到。",
             f"最新决策 {(dec or {}).get('decision_id', '—')}（{(dec or {}).get('time', '—')}）加权共识 S={(dec or {}).get('consensus')}：" if dec else "最新共识：无记录。"]
    for r, s in st.items():
        lines.append(f"- {ROLE_CN.get(r, r)}：{s.get('direction')} 置信 {s.get('confidence')} 目标 {s.get('target')} 止损 {s.get('stop')}")
    lines += ["", f"规则：只有止损未触发且持仓方向与最新共识仍一致时才可选 action=extend，并填 extend_days（≤{left_days:g} 天）；"
              "否则 action=close。延期不会改动止损止盈。理由用中文。"]
    return "\n".join(lines)


def _call_tm(cfg, prompt):
    """沿用交易经理的模型与角色手册（PlaybookLLM），经 llm_retry.call 重试 3 次。"""
    from desk import llm as dllm, llm_retry, roles
    tok = dllm.STAGE.set("持有期到期/交易经理")
    try:
        model = roles.PlaybookLLM(dllm.make_llm(cfg, "deep", None, role="trade_manager").with_structured_output(ExpiryDecision),
                                  "trade_manager")
        out = llm_retry.call(model.invoke, prompt, delays=ecfg(cfg)["retry_delays"], any_error=True)
        return out if isinstance(out, ExpiryDecision) else ExpiryDecision(**out)
    finally:
        dllm.STAGE.reset(tok)


def judge(cfg, inst: str, pos: dict, m: dict, now) -> dict:
    """返回 {"action": "extend"|"close", "days", "reason", "new_deadline"?}，并把结论写入 m["expiry_verdict"]（调用方负责保存登记）。
    同一 deadline 已有结论时直接返回该结论（重启不重复判断）。"""
    key = m.get("deadline")
    v = m.get("expiry_verdict")
    if v and v.get("deadline") == key:
        return v
    c = ecfg(cfg)

    def _done(action, reason, days=0.0, **kw):
        res = {"deadline": key, "action": action, "days": days, "reason": reason, "time": settings.now_str(), **kw}
        m["expiry_verdict"] = res
        return res

    if not c["enabled"]:
        return _done("close", "延期功能关闭")
    used_n, used_d = int(m.get("extensions") or 0), float(m.get("extended_days") or 0)
    left = float(c["max_extend_days"]) - used_d
    if used_n >= int(c["max_extensions"]) or left <= 0:
        return _done("close", f"已达延期上限（{used_n} 次/{used_d:g} 天）")
    # 跨轮尝试计数：轮次超时（SIGALRM）打断调用也算一次，超过上限回退平仓，避免永远不平
    att = m.setdefault("expiry_attempts", {})
    n = att.get(key, 0) + 1
    att.clear(); att[key] = n
    if n > int(c["max_round_attempts"]):
        return _done("close", f"交易经理连续 {n - 1} 轮未给出结论，回退到期平仓")
    dec = latest_decision(inst)
    mk = None
    try:
        from desk import manager
        mk = {k: round(v, 6) if isinstance(v, float) else v for k, v in manager.market_ctx(inst).items()}
    except Exception:
        mk = None
    try:
        out = _call_tm(cfg, _prompt(cfg, inst, pos, m, now, dec, mk, left))
    except Exception as e:
        return _done("close", f"交易经理调用失败（{type(e).__name__}），回退到期平仓", tm_failed=True)
    if out.action != "extend":
        return _done("close", f"交易经理选择平仓：{out.rationale[:100]}")
    if not consensus_agrees(cfg, pos.get("side"), (dec or {}).get("consensus")):
        return _done("close", f"交易经理选延期但最新共识 S={(dec or {}).get('consensus')} 与持仓方向不一致，按期平仓")
    try:
        days = min(float(out.extend_days or 0), left)
    except (TypeError, ValueError):
        days = 0
    if days <= 0:
        return _done("close", "交易经理选延期但 extend_days 无效，按期平仓")
    nd = (_dt.datetime.strptime(key, FMT) + _dt.timedelta(days=days)).strftime(FMT)
    return _done("extend", out.rationale[:100] or "交易经理选择延期", days=days, new_deadline=nd)


def apply_extend(m: dict, v: dict):
    """延期写回登记：更新 deadline，记次数/累计天数/原 deadline。止损止盈不动。"""
    m.setdefault("orig_deadline", m.get("deadline"))
    m["extensions"] = int(m.get("extensions") or 0) + 1
    m["extended_days"] = float(m.get("extended_days") or 0) + float(v["days"])
    m["deadline"] = v["new_deadline"]
    m["last_extension"] = {k: v.get(k) for k in ("deadline", "new_deadline", "days", "reason", "time")}
    m.pop("expiry_attempts", None)


def report_lines(date: str) -> list[str]:
    """日报/晚报：持有期到期：延期 X 天/平仓，原因。"""
    from desk import okx_demo as X
    out = []
    for e in settings.read_jsonl(X.EVENTS):
        if not str(e.get("time", "")).startswith(date):
            continue
        if e.get("event") == "expiry_extended" and e.get("mode") == "full_discussion":
            out.append(f"- 持有期到期：{e.get('inst_id')} 全员讨论结论{'持有' if e.get('outcome') == 'held' else '调整后持有'}"
                       f"（{e.get('final_action')}，共识 S={_fs(e.get('consensus'))}，决策 {e.get('decision_id')}），延期 {float(e.get('days') or 0):g} 天（新到期 {e.get('new_deadline')}）")
        elif e.get("event") == "expiry_closed" and e.get("mode") == "full_discussion":
            out.append(f"- 持有期到期：{e.get('inst_id')} 平仓，全员讨论{'结论平仓' if e.get('consensus') is not None else '未得出结论'}"
                       f"（共识 S={_fs(e.get('consensus'))}），原因：{e.get('reason')}")
        elif e.get("event") == "expiry_extended":
            out.append(f"- 持有期到期：{e.get('inst_id')} 延期 {float(e.get('days') or 0):g} 天（新到期 {e.get('new_deadline')}），原因：{e.get('reason')}")
        elif e.get("event") == "expiry_closed":
            out.append(f"- 持有期到期：{e.get('inst_id')} 平仓，原因：{e.get('reason')}")
    return out


def _fs(v):
    try:
        return f"{float(v):+.3f}"
    except (TypeError, ValueError):
        return "—"
