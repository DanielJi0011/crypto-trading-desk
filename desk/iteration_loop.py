"""自我迭代 v2 闭环收尾（2026-11-22 用户批准接上；版本冻结的用户批准例外）。

v2 三层里，评分诊断层一直在跑；本模块补上断掉的两环 + 一件欠账：
1. 案例自动提炼：平仓后把「开仓决策背景（判定/共识/交易经理理由）+ 实际结果」
   确定性拼装成一条案例追加进 state/cases.jsonl，格式与 5 条种子案例（C-01~C-05）同构，
   每条必带反例（同币反向结果的真实平仓；找不到如实标注无反例）。不新增 LLM 调用。
   水位（state/iteration_loop.json 的 case_trade_ids）保证同一平仓只提炼一次；
   首次初始化时，2026-11-21 00:00 之前的平仓视为历史基线不回填（早期教训已在种子案例里）。
2. 提案到量自动生成：按 role×环境格（regime×inst_id，与 scoring.env_skill_table 同口径）
   数自上批提案以来的新增已出结果评分；某格 ≥ learning.min_scored_before_edit（20）即达标，
   对达标角色复用 review 的提案机制（同一 LLM 审阅 + _write_proposal 落盘格式）生成建议，
   只落盘待用户批准，永不自动生效；每角色每批 ≤ learning.max_changes_per_edit 处改动。
   格水位存 iteration_loop.json；与旧路径的 proposals/last.json 取较晚者，避免同场重复生成。
3. v2 试行评估：对 playbook_changes.jsonl 中已上线的修订，按其 env_scope 与 baseline_hit，
   用修订后同环境已出结果评分现算（窗口 = 前 eval_window_after_edit 条），
   命中率低于基线 ≥ rollback_if_worse_pp 个百分点 → 标「触发回滚线」。
   本函数只算结论并落盘报告（state/iteration_reports/），不执行回滚、不改手册状态。

护栏（用户 2026-11-13 核准，不变）：权重冻结、评分只作诊断；提案永不自动生效；
learning 的 apply_weights / auto_edit_playbooks / research_write_playbook / ta_memory_inject
保持 false，本模块只在 playbook_proposals=true 时工作。
"""
from __future__ import annotations

import json
import re

from desk import review, roles, scoring, settings
from desk import llm as dllm
from desk.roles import ROLE_CN, ROLE_KEYS

CASES = settings.STATE / "cases.jsonl"
CLOSED = settings.STATE / "ledger" / "demo_closed.json"
DECISIONS = settings.STATE / "decisions.jsonl"
PB_LOG = settings.STATE / "playbook_changes.jsonl"
LOOP_STATE = settings.STATE / "iteration_loop.json"
REPORT_DIR = settings.STATE / "iteration_reports"

BASELINE_CUTOFF = "2026-11-21 00:00:00+0800"   # 闭环上线基线：此前平仓不回填提炼
STAGE_CN = {"late_exhaustion": "末段衰竭", "mid": "中段", "early": "初段"}


# ------------------------------------------------------------------ 水位
def _load_state() -> dict:
    st = settings.read_json(LOOP_STATE, {})
    st.setdefault("case_trade_ids", [])
    st.setdefault("cell_watermarks", {})
    return st


def _save_state(st: dict):
    st["updated"] = settings.now_str()
    settings.write_json(LOOP_STATE, st)


def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------ 1) 案例提炼
def _decision_index() -> dict:
    return {d["decision_id"]: d for d in settings.read_jsonl(DECISIONS) if d.get("decision_id")}


def _closed_trades() -> list[dict]:
    book = settings.read_json(CLOSED, {})
    rows = [t for t in (book.values() if isinstance(book, dict) else book)
            if isinstance(t, dict) and t.get("status") == "closed" and t.get("trade_id")]
    return sorted(rows, key=lambda t: str(t.get("exit_time", "")))


def _next_case_no(existing: list[dict]) -> int:
    mx = 0
    for c in existing:
        m = re.match(r"C-(\d+)$", str(c.get("id", "")))
        if m:
            mx = max(mx, int(m.group(1)))
    return mx + 1


def _counter_example(trade: dict, pool: list[dict]) -> str:
    """反例：优先同币、结果符号相反的真实平仓（取最近一笔）；其次任意币反向；都没有如实标注。"""
    pnl = _f(trade.get("pnl_usd"))
    if pnl == 0:
        return "无反例：本笔盈亏为零，本身不构成方向性证据；已检索全部已平仓账本，未据此提炼方向教训。"
    opps = [t for t in pool if t.get("trade_id") != trade.get("trade_id")
            and _f(t.get("pnl_usd")) != 0 and (_f(t.get("pnl_usd")) > 0) != (pnl > 0)]
    same = [t for t in opps if t.get("inst_id") == trade.get("inst_id")]
    pick = (same or opps)[-1] if (same or opps) else None
    if pick is None:
        return (f"无反例：已检索全部已平仓账本（{len(pool)} 笔），无结果相反的样本；"
                "本条为单样本观察，反例缺失本身即说明样本不足，不可据此改规则。")
    scope = "同币" if same else "跨币（同币无反向样本，降级引用）"
    return (f"反例（{scope}）：{str(pick.get('exit_time', ''))[:10]} {pick.get('inst_id')} 一笔平仓"
            f"（决策 {pick.get('decision_id') or pick.get('signal_id') or '无关联'}）结果相反："
            f"{_f(pick.get('pnl_usd')):+.2f} USDT（{pick.get('exit_reason') or '平仓'}）；"
            "说明结果取决于入场阶段与点位，本条不可单独推广。")


def build_case(trade: dict, dec: dict | None, pool: list[dict], score_ids: list[str]) -> dict:
    """确定性拼装一条案例，键与种子案例完全同构。"""
    judge = ((dec or {}).get("llm_regime") or {}).get("judge") or {}
    regime = None
    if judge:
        regime = {"direction": judge.get("direction"), "strength": judge.get("strength"),
                  "stage": judge.get("stage"), "confidence": judge.get("confidence")}
    mgr = (dec or {}).get("manager") or {}
    side_cn = "空" if trade.get("side") == "sell" else "多"
    entry, exit_px = _f(trade.get("entry_px")), _f(trade.get("exit_px"))
    sl, tp = _f(trade.get("sl_px")), _f(trade.get("tp_px"))
    pnl, pnl_pct = _f(trade.get("pnl_usd")), _f(trade.get("pnl_pct"))
    stage = (regime or {}).get("stage") or "未知阶段"
    stage_cn = STAGE_CN.get(stage, stage)
    tp_txt = f"{tp:g}" if tp else "无（移动止损）"
    if dec:
        context = (f"{dec.get('date', '')} {dec.get('session', '')} 决策 {dec.get('decision_id')}："
                   f"共识 S={_f(dec.get('consensus')):+.3f}（{dec.get('consensus_side') or ''}），"
                   f"判定 {judge.get('direction') or '无'}/{judge.get('strength') or '—'}/{stage}"
                   f"（置信 {judge.get('confidence')}）；交易经理 {mgr.get('action') or '—'}"
                   f"（{dec.get('decision_type') or '—'}），{_f(trade.get('contracts')):g} 张 @ {entry:g} 成交"
                   f"（{trade.get('entry_time', '')}），SL {sl:g} / TP {tp_txt}，"
                   f"risk_scale {mgr.get('risk_scale')}。理由：{str(mgr.get('rationale') or '')[:240]}")
        decision = (f"按交易经理决策执行（{mgr.get('action') or '—'}）；失效条件："
                    f"{str(mgr.get('invalidation') or '未记录')[:160]}")
    else:
        context = (f"账本平仓（决策记录缺失，signal_id={trade.get('signal_id') or '无'}）："
                   f"{trade.get('inst_id')} {side_cn}单 {_f(trade.get('contracts')):g} 张 @ {entry:g} 成交"
                   f"（{trade.get('entry_time', '')}），SL {sl:g} / TP {tp_txt}。")
        decision = "无关联决策记录，仅按账本事实提炼，不评价决策质量。"
    outcome = (f"{trade.get('exit_time', '')} 以「{trade.get('exit_reason') or '平仓'}」平仓 @ {exit_px:g}，"
               f"盈亏 {pnl:+.2f} USDT（{pnl_pct:+.2f}%；毛盈亏 {_f(trade.get('gross_pnl_usd')):+.2f}、"
               f"资金费 {_f(trade.get('funding_usd')):+.2f}、手续费 {_f(trade.get('fee_usd')):.2f}）。")
    if pnl > 0:
        lesson = (f"本笔在{stage_cn}阶段按计划兑现盈利（{pnl:+.2f}）：开仓判定 {stage}、"
                  f"共识 S={_f((dec or {}).get('consensus')):+.3f}、risk_scale {mgr.get('risk_scale')} 的组合在本样本中执行到位。"
                  "单样本只作观察记录，不构成改规则依据；需同格样本积累后由提案机制评估。")
    elif pnl < 0:
        lesson = (f"本笔亏损出场（{pnl:+.2f}，出场原因「{trade.get('exit_reason') or '平仓'}」）：开仓判定 {stage}、"
                  f"止损距离 {abs(entry - sl) / entry * 100:.2f}%（SL {sl:g}）未能覆盖期间反向波动，或入场点位贴近反转区。"
                  "单样本只作观察记录；若同阶段亏损样本累积，优先由提案机制评估止损缓冲与入场点位，而非单笔归因。")
    else:
        lesson = "本笔盈亏为零，不提炼方向性教训，仅留档。"
    return {"id": "",   # 由 distill_cases 统一编号
            "date": (dec or {}).get("date") or str(trade.get("exit_time", ""))[:10],
            "session": (dec or {}).get("session", ""),
            "inst_id": trade.get("inst_id"),
            "regime": regime,
            "context": context,
            "decision": decision,
            "outcome": {"text": outcome, "score_ids": score_ids},
            "lesson": lesson,
            "counter_example": _counter_example(trade, pool),
            "status": "观察",
            "playbook_rule": ""}


def distill_cases(cfg=None) -> list[dict]:
    """把未提炼的新平仓追加为案例；返回本次新增案例。同一平仓只提炼一次（水位）。"""
    st = _load_state()
    done = set(st["case_trade_ids"])
    trades = _closed_trades()
    if not st.get("initialized"):
        # 首次上线基线：cutoff 之前的历史平仓不回填（早期教训已在种子案例 C-01~C-05）
        done |= {t["trade_id"] for t in trades if str(t.get("exit_time", "")) < BASELINE_CUTOFF}
        st["initialized"] = True
    pending = [t for t in trades if t["trade_id"] not in done]
    if not pending:
        _save_state(st)
        return []
    decs = _decision_index()
    scores_by_dec: dict[str, list[str]] = {}
    for s in scoring.scores():
        if s.get("decision_id"):
            scores_by_dec.setdefault(s["decision_id"], []).append(s["id"])
    existing = settings.read_jsonl(CASES)
    no = _next_case_no(existing)
    new_cases = []
    for t in pending:
        did = t.get("decision_id") or t.get("signal_id") or ""
        case = build_case(t, decs.get(did), trades, scores_by_dec.get(did, []))
        case["id"] = f"C-{no:02d}"
        no += 1
        settings.append_jsonl(CASES, case)
        done.add(t["trade_id"])
        new_cases.append(case)
    st["case_trade_ids"] = sorted(done)
    _save_state(st)
    return new_cases


# ------------------------------------------------------------------ 2) 提案到量生成
def _cell_key(role: str, regime, inst: str) -> str:
    return f"{role}|{regime}|{inst}"


def _last_proposal_time(role: str) -> str:
    return settings.read_json(review.proposals_dir(role) / "last.json", {}).get("time", "")


def due_cells(cfg, st: dict | None = None) -> list[dict]:
    """各 role×（regime×inst）格自上批提案以来的新增已出结果评分；≥门槛的格达标。"""
    st = st if st is not None else _load_state()
    need = int(cfg["learning"]["min_scored_before_edit"])
    out = []
    for role in ROLE_KEYS:
        lp = _last_proposal_time(role)
        cells: dict[tuple, list[dict]] = {}
        for s in scoring.scores(role):
            wm = max(st["cell_watermarks"].get(_cell_key(role, s.get("regime"), s.get("inst_id")), ""), lp)
            if s["time"] > wm:
                cells.setdefault((s.get("regime"), s.get("inst_id")), []).append(s)
        for (regime, inst), xs in sorted(cells.items()):
            if len(xs) >= need:
                out.append({"role": role, "regime": regime, "inst_id": inst, "n": len(xs),
                            "hit": sum(x["hit"] for x in xs) / len(xs), "samples": xs})
    return out


def generate_batch(cfg, tracker, st: dict | None = None) -> list[dict]:
    """对有达标格的角色生成修订建议（只落盘待批）。返回与日报 pb_events 同构的事件列表。"""
    L = cfg["learning"]
    st = st if st is not None else _load_state()
    due = due_cells(cfg, st)
    by_role: dict[str, list[dict]] = {}
    for c in due:
        by_role.setdefault(c["role"], []).append(c)
    events = []
    for role, cells in sorted(by_role.items()):
        fresh = [s for c in cells for s in c["samples"]]
        fresh.sort(key=lambda s: s["time"])
        scope = review.env_scope_of(fresh)
        card = scoring.scorecard(role)
        cells_desc = "；".join(f"{c['regime']}/{c['inst_id']} 新增 {c['n']} 条（命中 {c['hit']:.0%}）" for c in cells)
        misses = [s for s in fresh if not s["hit"]][-15:]
        prompt = (f"你是交易台的交易经理，正在修订【{ROLE_CN[role]}】的角色手册（经验规则部分）。\n"
                  f"规则：最多改 {L['max_changes_per_edit']} 处；只有统计证据清楚时才改，否则 no_change=true；"
                  "规则要具体可执行（例如'在 20 日跌幅>5% 的下跌市中，看多置信度不超过 0.5'），不要空话；不得修改'输出要求'部分。\n\n"
                  f"## 当前手册\n{roles.current_text(role)}\n\n"
                  f"## 成绩单（全部 {card['n']} 条，最近 20 条命中率 {card['recent']['hit']}）\n{dllm.dumps(card)}\n\n"
                  f"## 上次修订建议以来已达标的环境格：{cells_desc}\n## 这些格中 {len(fresh)} 条打分中的失误\n"
                  + "\n".join(f"- {s['date']} {s['inst_id']} 市况{s['regime']} 观点{s['direction']}(置信{s['confidence']}) "
                              f"结果{s['outcome']}({s['ret_pct']:+.2f}%) 止损:{s['stop_quality']}" for s in misses)
                  + "\n\n## 复盘会教训\n" + ("\n".join(review.recent_lessons(role)) or "（无）"))
        tok = dllm.STAGE.set(f"手册修订/{role}")
        try:
            ed = dllm.make_llm(cfg, "deep", tracker, role="reviewer").with_structured_output(review.PlaybookEdit).invoke(prompt)
            ed = ed if isinstance(ed, review.PlaybookEdit) else review.PlaybookEdit(**ed)
        except Exception as e:
            events.append({"role": role, "type": "修订失败", "error": type(e).__name__})
            continue   # 不推进水位，下次重试
        finally:
            dllm.STAGE.reset(tok)
        if not ed.no_change and len(ed.changes) > int(L["max_changes_per_edit"]):
            ev = {"role": role, "type": "拒绝修订",
                  "why": f"改动 {len(ed.changes)} 处超过上限 {L['max_changes_per_edit']}"}
        else:
            ev = review._write_proposal(role, ed, len(fresh), scope)
        ev["time"] = settings.now_str()
        settings.append_jsonl(PB_LOG, ev)
        events.append(ev)
        for c in cells:   # 该角色达标格水位推进（已审阅；失败分支上面已 continue，不到这里）
            st["cell_watermarks"][_cell_key(role, c["regime"], c["inst_id"])] = ev["time"]
    if by_role:
        _save_state(st)
    return events


def batch_summary(events: list[dict], due: list[dict]) -> str:
    """批次摘要（供主代理直接转述用户）：达标格 + 每条提案的去向。"""
    lines = [f"达标格 {len(due)} 个：" + "；".join(
        f"{ROLE_CN.get(c['role'], c['role'])} {c['regime']}/{c['inst_id']} 新增 {c['n']} 条（命中 {c['hit']:.0%}）" for c in due)]
    for e in events:
        if e.get("type") == "建议":
            lines.append(f"- {ROLE_CN.get(e['role'], e['role'])}：建议 {len(e.get('changes', []))} 处 → "
                         f"playbooks/{e['role']}/proposals/{e.get('file')}（未生效，待批准）")
        else:
            lines.append(f"- {ROLE_CN.get(e.get('role'), e.get('role'))}：{e.get('type')} {e.get('why', '') or e.get('error', '')}")
    return "\n".join(lines)


# ------------------------------------------------------------------ 3) v2 试行评估（只算结论）
def evaluate_trial(cfg) -> dict:
    """按 playbook_changes.jsonl 的修订记录现算试行结论；只落盘报告，不执行回滚、不改手册。"""
    L = cfg["learning"]
    window = int(L["eval_window_after_edit"])
    pp = float(L["rollback_if_worse_pp"])
    rows = []
    for ch in settings.read_jsonl(PB_LOG):
        if ch.get("type") != "修订" or ch.get("baseline_hit") is None:
            continue
        role, base, scope, t0 = ch["role"], float(ch["baseline_hit"]), ch.get("env_scope"), ch.get("time", "")
        pool = review._scoped([s for s in scoring.scores(role) if s["time"] > t0], scope)
        win = pool[:window]
        post = sum(x["hit"] for x in win) / len(win) if len(win) >= window else None
        post_all = sum(x["hit"] for x in pool) / len(pool) if pool else None
        if post is None:
            verdict = f"样本不足（{len(pool)}/{window}）"
            diff = None
        else:
            diff = round((post - base) * 100, 1)
            verdict = "触发回滚线（待用户决定）" if post < base - pp / 100 else "达标保留"
        rows.append({"role": role, "version": ch.get("version"),
                     "scope": (f"{scope['regime']}/{scope['inst_id']}" if scope else "全局"),
                     "baseline_hit": base, "n": len(pool), "window": window,
                     "post_hit": post, "post_hit_all": post_all, "diff_pp": diff, "verdict": verdict})
    lines = ["# v2 剧本试行评估（自我迭代闭环，只算结论、不执行回滚）",
             f"\n生成：{settings.now_str()}",
             f"口径：样本 = 修订时间（2026-11-15 上线）之后、同 env_scope 环境的已出结果评分；"
             f"窗口命中率取前 {window} 条；基线为修订时记录的 baseline_hit；"
             f"窗口命中率低于基线 ≥ {pp:g} 个百分点 → 触发回滚线（回滚与否由用户决定，本报告不执行）。",
             "\n| 角色 | 版本 | 环境 | 基线 | 样本数 | 窗口命中率 | 全部命中率 | 差值(pp) | 结论 |",
             "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append("| {} | v{} | {} | {:.0%} | {} | {} | {} | {} | {} |".format(
            ROLE_CN.get(r["role"], r["role"]), r["version"], r["scope"], r["baseline_hit"], r["n"],
            f"{r['post_hit']:.0%}" if r["post_hit"] is not None else "—",
            f"{r['post_hit_all']:.0%}" if r["post_hit_all"] is not None else "—",
            f"{r['diff_pp']:+.1f}" if r["diff_pp"] is not None else "—", r["verdict"]))
    text = "\n".join(lines) + "\n"
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"trial-eval-{settings.now_str()[:10]}.md"
    path.write_text(text, encoding="utf-8")
    return {"rows": rows, "path": str(path), "text": text}


# ------------------------------------------------------------------ 收尾入口
def run_tail(cfg, tracker=None) -> dict:
    """run_daily 收尾调用：案例提炼 → 提案到量生成 → 试行评估。只落盘，不生效、不回滚。"""
    out = {"cases": [], "events": [], "trial": None}
    if not cfg["learning"].get("playbook_proposals", False):
        return out
    st = _load_state()
    out["cases"] = distill_cases(cfg)
    if tracker is not None:
        out["events"] = generate_batch(cfg, tracker, st)
    out["trial"] = evaluate_trial(cfg)
    return out


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="自我迭代 v2 闭环收尾（只落盘，不生效）")
    ap.add_argument("--cases", action="store_true", help="只跑案例提炼")
    ap.add_argument("--proposals", action="store_true", help="只跑提案到量生成（调用 LLM）")
    ap.add_argument("--trial", action="store_true", help="只跑 v2 试行评估")
    a = ap.parse_args(argv)
    cfg = settings.load_config()
    all_ = not (a.cases or a.proposals or a.trial)
    if a.cases or all_:
        new = distill_cases(cfg)
        print(f"[iteration] 案例提炼：新增 {len(new)} 条")
        for c in new:
            print(json.dumps(c, ensure_ascii=False))
    if a.proposals or all_:
        st = _load_state()
        due = due_cells(cfg, st)
        print(f"[iteration] 达标格：{len(due)}")
        events = generate_batch(cfg, dllm.UsageTracker(cfg), st)
        print(batch_summary(events, due))
    if a.trial or all_:
        rep = evaluate_trial(cfg)
        print(rep["text"])
        print(f"[iteration] 评估报告：{rep['path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
