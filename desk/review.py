"""复盘会与角色手册修订（含防过拟合规则与自动回滚）。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from desk import roles, scoring, settings
from desk import llm as dllm
from desk.roles import ROLE_CN, ROLE_KEYS

REVIEWS = settings.STATE / "reviews"
DECISIONS = settings.STATE / "decisions.jsonl"
PB_LOG = settings.STATE / "playbook_changes.jsonl"


def decision_by_id(did):
    return next((d for d in reversed(settings.read_jsonl(DECISIONS)) if d["decision_id"] == did), None)


# ------------------------------------------------------------------ 复盘会
class RoleVerdict(BaseModel):
    role: str = Field(description="角色键名")
    verdict: Literal["正确", "错误", "中性/不适用"]
    lesson: str = Field(description="一句话教训（≤50字），没有就写'无'")


class TradeReview(BaseModel):
    summary: str = Field(description="本笔交易复盘总结（中文，≤200字）")
    verdicts: list[RoleVerdict]
    manager_lesson: str = Field(description="交易经理自身（仓位/止损/执行）的教训，≤80字")


def role_call_table(trade, dec) -> list[dict]:
    win = float(trade["pnl_usd"]) > 0
    short = trade.get("side") in ("sell", "short")
    rows = []
    for r in ROLE_KEYS:
        s = dec["stances"][r] if dec else {"direction": "neutral"}
        d = s["direction"]
        # 与交易同向的观点在盈利时算对；空单（side=sell）亏损时看空算错、看多算对
        right = None if d in ("neutral", "close") else (((d == "bullish") == (not short)) == win)
        rows.append({"role": r, "direction": d, "confidence": s.get("confidence"), "target": s.get("target"),
                     "stop": s.get("stop"), "right": right})
    return rows


def tm_line(trade, dec) -> dict:
    """方案一：复盘主线 = 交易经理这一单（当时动作、单子类型、定的点位、结果）。"""
    m = (dec or {}).get("manager") or {}
    from desk import tm_ledger
    return {"decision_id": (dec or {}).get("decision_id"), "action": m.get("action"),
            "decision_type": (dec or {}).get("decision_type"), "final_action": (dec or {}).get("final_action"),
            "consensus": (dec or {}).get("consensus"), "vetoed_consensus": bool((dec or {}).get("tm_vetoed_consensus")),
            "entry_px": trade.get("entry_px"), "sl_px": trade.get("sl_px"), "tp_px": trade.get("tp_px"), "exit_px": trade.get("exit_px"),
            "exit_reason": trade.get("exit_reason"), "pnl_usd": trade.get("pnl_usd"), "pnl_pct": trade.get("pnl_pct"),
            "realized_rr": tm_ledger.realized_rr(trade), "rationale": m.get("rationale")}


def review_trade(cfg, trade, tracker) -> dict:
    dec = decision_by_id(trade.get("signal_id"))
    table = role_call_table(trade, dec)
    from desk import tm_ledger
    tml = tm_line(trade, dec) if tm_ledger.enabled(cfg) else None
    prompt = [f"交易复盘会。标的 {trade['inst_id']}（OKX 永续 {'空' if trade.get('side') == 'sell' else '多'}单），入场 {trade['entry_time']} @ {trade['entry_px']}，"
              f"止损 {trade['sl_px']}，止盈 {trade['tp_px'] or '无'}；出场 {trade['exit_time']} @ {trade['exit_px']}"
              f"（{trade['exit_reason']}），盈亏 {float(trade['pnl_usd']):+.2f} USDT（{float(trade['pnl_pct']):+.2f}%，已含手续费）。",
              f"入场理由：{trade.get('entry_reason', '')}", "", "当时各角色观点："]
    for t in table:
        prompt.append(f"- {t['role']}（{ROLE_CN[t['role']]}）：{t['direction']} 置信 {t['confidence']} 目标 {t['target']} 止损 {t['stop']}")
    if dec:
        prompt.append(f"交易经理当时理由：{dec['manager'].get('rationale', '')}；失效条件：{dec['manager'].get('invalidation', '')}")
    if tml:
        prompt += ["", "【复盘主线：交易经理】这一单由交易经理定入场价、止损、止盈和仓位（方向由投票角色共识决定），盈亏记在交易经理名下。",
                   f"交易经理当时动作 {tml['action']}（单子类型 {tml['decision_type'] or '—'}，最终执行 {tml['final_action']}，共识 S={tml['consensus']}），"
                   f"入场 {tml['entry_px']} 止损 {tml['sl_px']} 止盈 {tml['tp_px'] or '无'}，出场 {tml['exit_px']}（{tml['exit_reason']}），"
                   f"实际盈亏比 {tml['realized_rr']}。请先评价交易经理的点位、仓位和执行，把结论写进 summary 与 manager_lesson；"
                   "其他角色的方向对错只作参考。"]
    prompt.append("\n请逐个角色判定其观点对这笔交易是否正确，并给一句可操作的教训；不要事后诸葛亮，只评价当时可得信息下的判断质量。")
    tok = dllm.STAGE.set(f"{trade['inst_id']}/复盘会")
    try:
        res = dllm.make_llm(cfg, "deep", tracker, role="reviewer").with_structured_output(TradeReview).invoke("\n".join(prompt))
        res = res.model_dump() if hasattr(res, "model_dump") else dict(res)
    except Exception as e:
        res = {"summary": f"复盘 LLM 调用失败（{type(e).__name__}），仅保留代码统计", "verdicts": [], "manager_lesson": ""}
    finally:
        dllm.STAGE.reset(tok)
    out = {"trade_id": trade["trade_id"], "time": settings.now_str(), "trade": trade, "table": table, **res}
    if tml:
        out["tm_line"] = tml
    REVIEWS.mkdir(parents=True, exist_ok=True)
    settings.write_json(REVIEWS / f"{trade['trade_id']}.json", out)
    return out


def recent_lessons(role, n=10) -> list[str]:
    out = []
    for p in sorted(REVIEWS.glob("*.json"), key=lambda p: p.stat().st_mtime)[-50:]:
        rv = settings.read_json(p, {})
        for v in rv.get("verdicts", []):
            if v.get("role") == role and v.get("lesson") not in (None, "", "无"):
                out.append(f"[{rv['trade']['inst_id']} {v['verdict']}] {v['lesson']}")
    return out[-n:]


# ------------------------------------------------------------------ 手册修订
class Change(BaseModel):
    op: Literal["新增", "修改", "删除"]
    rule: str = Field(description="规则内容（修改/删除时写原规则与新规则）")
    why: str = Field(description="依据哪些统计/复盘得出（引用数字）")


class PlaybookEdit(BaseModel):
    no_change: bool = Field(description="证据不足以改动时为 true")
    changes: list[Change] = Field(default_factory=list, description="最多 2 处")
    learned_rules: str = Field(default="", description="修订后的完整'经验规则'段落（markdown 列表）")


def _hit(xs):
    return sum(x["hit"] for x in xs) / len(xs) if xs else None


def _since(role, t):
    return [s for s in scoring.scores(role) if s["time"] > t]


def evaluate_and_rollback(cfg) -> list[dict]:
    L = cfg["learning"]
    if L.get("auto_edit_playbooks", False):
        mode = "edit"
    elif L.get("playbook_proposals", False):
        mode = "propose"   # L1：只写 playbooks/<role>/proposals/，不切换版本、不进观察期
    else:
        return []          # 学习冻结（L1）：不评估、不回滚、不修订，不调用 LLM
    events = []
    for r in ROLE_KEYS:
        m = roles.read_meta(r)
        cur = next(h for h in m["history"] if h["version"] == m["current"])
        if cur.get("eval_status") != "pending":
            continue
        after = _since(r, cur["time"])
        if len(after) < L["eval_window_after_edit"]:
            continue
        post = _hit(after[:L["eval_window_after_edit"]])
        base = cur.get("baseline_hit")
        if base is not None and post < base - L["rollback_if_worse_pp"] / 100:
            prev = cur.get("previous") or 1
            learned = roles.learned_section(roles.version_path(r, prev).read_text(encoding="utf-8"))
            cur["eval_status"] = "rolled_back"; cur["post_hit"] = post
            settings.write_json(roles.meta_path(r), m)
            v = roles.save_new_version(r, learned, f"自动回滚：v{cur['version']:03d} 后 {len(after[:20])} 条命中率 {post:.0%} "
                                       f"低于修订前 {base:.0%} 超过 {L['rollback_if_worse_pp']}pp", [], post, 0,
                                       len(scoring.scores(r)), rollback_of=cur["version"])
            ev = {"role": r, "type": "回滚", "from": cur["version"], "to": v, "base": base, "post": post}
        else:
            cur["eval_status"] = "kept"; cur["post_hit"] = post
            settings.write_json(roles.meta_path(r), m)
            ev = {"role": r, "type": "保留", "version": cur["version"], "base": base, "post": post}
        ev["time"] = settings.now_str()
        settings.append_jsonl(PB_LOG, ev)
        events.append(ev)
    return events


def proposals_dir(role: str):
    return roles.role_dir(role) / "proposals"


def _last_proposal(role: str) -> str:
    return settings.read_json(proposals_dir(role) / "last.json", {}).get("time", "")


def _write_proposal(role: str, ed, n_fresh: int) -> dict:
    """L1：修订建议只落盘，不改 meta.json、不生成新版本。"""
    d = proposals_dir(role); d.mkdir(parents=True, exist_ok=True)
    ts = settings.now_str()
    if ed.no_change or not ed.changes or not ed.learned_rules.strip():
        ev = {"role": role, "type": "建议无改动", "n_fresh": n_fresh}
    else:
        f = d / f"{ts[:10]}-{ts[11:19].replace(':', '')}.md"
        f.write_text(f"# {ROLE_CN.get(role, role)} 手册修订建议（未生效，需用户确认）\n\n时间：{ts}\n\n## 改动\n"
                     + "\n".join(f"- {c.op}：{c.rule}（依据：{c.why}）" for c in ed.changes)
                     + "\n\n## 建议的经验规则全文\n" + ed.learned_rules.strip() + "\n", encoding="utf-8")
        ev = {"role": role, "type": "建议", "file": str(f.name), "changes": [c.model_dump() for c in ed.changes]}
    settings.write_json(d / "last.json", {"time": ts})
    return ev


def maybe_edit_playbooks(cfg, tracker) -> list[dict]:
    L = cfg["learning"]
    if L.get("auto_edit_playbooks", False):
        mode = "edit"
    elif L.get("playbook_proposals", False):
        mode = "propose"   # L1：只写 playbooks/<role>/proposals/，不切换版本、不进观察期
    else:
        return []          # 学习冻结（L1）：不评估、不回滚、不修订，不调用 LLM
    events = []
    for r in ROLE_KEYS:
        m = roles.read_meta(r)
        cur = next(h for h in m["history"] if h["version"] == m["current"])
        if cur.get("eval_status") == "pending":
            continue                       # 正在观察期，不叠加修改
        t0 = max(cur["time"], m.get("last_review", ""), _last_proposal(r) if mode == "propose" else "")
        fresh = _since(r, t0)
        if len(fresh) < L["min_scored_before_edit"]:
            continue
        allsc = scoring.scores(r)
        card = scoring.scorecard(r)
        misses = [s for s in fresh if not s["hit"]][-15:]
        text = roles.current_text(r)
        prompt = (f"你是交易台的交易经理，正在修订【{ROLE_CN[r]}】的角色手册（经验规则部分）。\n"
                  f"规则：最多改 {L['max_changes_per_edit']} 处；只有统计证据清楚时才改，否则 no_change=true；"
                  "规则要具体可执行（例如'在 20 日跌幅>5% 的下跌市中，看多置信度不超过 0.5'），不要空话；不得修改'输出要求'部分。\n\n"
                  f"## 当前手册\n{text}\n\n## 成绩单（全部 {card['n']} 条，最近 20 条命中率 {card['recent']['hit']}）\n"
                  f"{dllm.dumps(card)}\n\n## 上次修订以来的 {len(fresh)} 条打分中的失误\n"
                  + "\n".join(f"- {s['date']} {s['inst_id']} 市况{s['regime']} 观点{s['direction']}(置信{s['confidence']}) "
                              f"结果{s['outcome']}({s['ret_pct']:+.2f}%) 止损:{s['stop_quality']}" for s in misses)
                  + "\n\n## 复盘会教训\n" + ("\n".join(recent_lessons(r)) or "（无）"))
        tok = dllm.STAGE.set(f"手册修订/{r}")
        try:
            ed = dllm.make_llm(cfg, "deep", tracker, role="reviewer").with_structured_output(PlaybookEdit).invoke(prompt)
            ed = ed if isinstance(ed, PlaybookEdit) else PlaybookEdit(**ed)
        except Exception as e:
            ed = PlaybookEdit(no_change=True); events.append({"role": r, "type": "修订失败", "error": type(e).__name__})
        finally:
            dllm.STAGE.reset(tok)
        if mode == "propose":
            ev = _write_proposal(r, ed, len(fresh))
            ev["time"] = settings.now_str()
            settings.append_jsonl(PB_LOG, ev)
            events.append(ev)
            continue
        m = roles.read_meta(r); m["last_review"] = settings.now_str(); settings.write_json(roles.meta_path(r), m)
        if ed.no_change or not ed.changes or not ed.learned_rules.strip():
            ev = {"role": r, "type": "审阅无改动", "n_fresh": len(fresh)}
        elif len(ed.changes) > L["max_changes_per_edit"]:
            ev = {"role": r, "type": "拒绝修订", "why": f"改动 {len(ed.changes)} 处超过上限 {L['max_changes_per_edit']}"}
        else:
            base = _hit(allsc[-L["eval_window_after_edit"]:])
            v = roles.save_new_version(r, ed.learned_rules, "依据打分与复盘修订", [c.model_dump() for c in ed.changes],
                                       base, min(len(allsc), L["eval_window_after_edit"]), len(allsc))
            ev = {"role": r, "type": "修订", "version": v, "baseline_hit": base, "changes": [c.model_dump() for c in ed.changes]}
        ev["time"] = settings.now_str()
        settings.append_jsonl(PB_LOG, ev)
        events.append(ev)
    return events
