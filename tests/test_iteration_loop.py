"""自我迭代 v2 闭环（2026-11-22 接上）：案例提炼 / 提案到量生成 / v2 试行评估。

夹具纪律（2026-11-21 质量门）：字典键分布与生产一致——
- 打分行 = scoring.score_due 写出的完整键集（hit 非空才算已出结果）；
- 平仓行 = demo_closed.json 的真实键集（价格为字符串、decision_id/signal_id 关联决策）；
- 决策行 = decisions.jsonl 的键集（判定在 llm_regime.judge、理由在 manager.rationale、共识在顶层）；
- 修订行 = playbook_changes.jsonl 的键集（env_scope/baseline_hit/time）。
全部离线：LLM 用假实现，状态文件写进临时 STATE。
"""
import json

from desk import iteration_loop as IL
from desk import review, roles, settings

CFG = {"learning": {"min_scored_before_edit": 20, "max_changes_per_edit": 2,
                    "eval_window_after_edit": 20, "rollback_if_worse_pp": 5.0,
                    "weight_clip": [0.5, 2.0], "apply_weights": False,
                    "auto_edit_playbooks": False, "research_write_playbook": False,
                    "ta_memory_inject": False, "playbook_proposals": True}}

SEED_KEYS = {"id", "date", "session", "inst_id", "regime", "context", "decision",
             "outcome", "lesson", "counter_example", "status", "playbook_rule"}


def _score(role, regime, inst, t, hit=True, dec="D-20261117-ETH"):
    return {"id": f"{dec}:{role}", "decision_id": dec, "role": role, "inst_id": inst,
            "date": t[:10], "time": t, "regime": regime, "direction": "bearish",
            "confidence": 0.6, "ret_pct": -3.0 if hit else 2.0,
            "outcome": "跌" if hit else "涨", "hit": hit, "stop_quality": "未触及",
            "target_hit": None, "horizon": 5, "band_pct": 1.5, "band_src": "atr",
            "triggered": None, "base_rate": 0.4, "skill": 0.1, "brier": 0.2,
            "plan_r": None, "plan_exit": "no_plan", "scoring_v": 2,
            "scored_at": t, "rules_cited": []}


def _trade(tid, inst, dec_id, entry_t, exit_t, entry_px, exit_px, pnl, reason="止盈"):
    return {"trade_id": tid, "status": "closed", "mode": "okx_demo", "venue": "okx_demo",
            "inst_id": inst, "inst_type": "SWAP", "side": "sell", "pos_side": "short",
            "sz": "7.544", "contracts": "75.44", "notional_usd": "13317.93", "leverage": 1.0,
            "entry_time": entry_t, "entry_px": str(entry_px), "sl_px": "1920",
            "tp_px": "1670", "exit_time": exit_t, "exit_px": str(exit_px),
            "exit_reason": reason, "fee_usd": "11.45", "funding_usd": "41.59",
            "gross_pnl_usd": str(pnl), "pnl_usd": pnl, "pnl_pct": 9.82,
            "signal_id": dec_id, "decision_id": dec_id, "close_type": 2,
            "pos_id": "4106742384121556994", "link_source": "entry_limit(4106076879818502144)",
            "entry_reason": ""}


def _decision(did, date, session, inst, stage, consensus=-0.27):
    return {"decision_id": did, "date": date, "time": f"{date} 07:50:27+0800",
            "session": session, "inst_id": inst, "consensus": consensus,
            "consensus_pre_tm": consensus, "consensus_side": "short",
            "final_action": "pending", "decision_type": "挂条件单", "source": "daily",
            "price": 1819.38, "ta_signal": "Sell", "run_path": "正常",
            "llm_regime": {"judge": {"direction": "short", "strength": "strong",
                                     "stage": stage, "confidence": 0.6,
                                     "evidence": ["均线空头排列"]}},
            "judge_texts": {}, "stances": {}, "weights": {}, "market": {},
            "manager": {"action": "pending", "confidence": 0.6, "rationale": "共识偏空，近价挂单。",
                        "invalidation": "站回 1900 上方", "risk_scale": 0.5,
                        "entry": None, "entry_condition": "gte", "entry_trigger_px": 1843,
                        "entry_stop_loss": 1920, "entry_take_profit": 1670,
                        "entry_side": "sell", "entry_size_pct": None, "entry_expire_hours": 36,
                        "horizon_days": 5, "leverage": 1, "new_stop": None, "new_tp": None,
                        "stop_loss": None, "take_profit": None, "reduce_fraction": None},
            "tm_vetoed_consensus": False, "tm_status": "ok", "tm_absent_reason": None,
            "tm_flip_blocked": False, "tm_order_tool": {}, "execution": {}, "pending": {},
            "add_preview": None, "aggregate": {}, "analysis_px": None, "analysis_px_time": None,
            "config_hash": "x", "decided_by": "tm", "drift_check": {}, "drift_skipped": False,
            "notes": [], "playbook_versions": {}, "stance_field_conflicts": [], "ticker": {}}


def _seed_case(cid):
    return {"id": cid, "date": "2026-11-11", "session": "morning", "inst_id": "ETH-USDT-SWAP",
            "regime": {"direction": "short", "strength": "strong", "stage": "mid", "confidence": 0.6},
            "context": "种子上下文", "decision": "种子决策",
            "outcome": {"text": "种子结果", "score_ids": []}, "lesson": "种子教训",
            "counter_example": "种子反例", "status": "观察", "playbook_rule": ""}


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def _setup_book():
    """账本：1 笔基线前平仓（不回填）+ 2 笔新平仓（1 盈 1 亏）+ 对应决策与打分。"""
    old = _trade("DEMO-OLD", "BTC-USDT-SWAP", "D-20261119-BTC",
                 "2026-11-15 10:31:30+0800", "2026-11-19 23:16:32+0800", 64714.7, 60208.8, 721.09)
    win = _trade("DEMO-WIN", "ETH-USDT-SWAP", "D-20261117-ETH",
                 "2026-11-17 13:21:00+0800", "2026-11-21 11:32:30+0800", 1765.37, 1595.96, 1308.13)
    loss = _trade("DEMO-LOSS", "ETH-USDT-SWAP", "D-20261121-ETH-2",
                  "2026-11-21 14:44:06+0800", "2026-11-21 21:26:48+0800", 1610.61, 1676.8, -52.58,
                  reason="平仓")
    settings.write_json(IL.CLOSED, {t["trade_id"]: t for t in (old, win, loss)})
    _write_jsonl(IL.DECISIONS, [_decision("D-20261117-ETH", "2026-11-17", "morning",
                                          "ETH-USDT-SWAP", "late_exhaustion"),
                                _decision("D-20261121-ETH-2", "2026-11-21", "rediscuss",
                                          "ETH-USDT-SWAP", "mid")])
    _write_jsonl(IL.CASES, [_seed_case("C-01"), _seed_case("C-02")])
    _write_jsonl(settings.STATE / "scores.jsonl",
                 [_score("market", "下跌", "ETH-USDT-SWAP", "2026-11-17 07:50:27+0800", True)])
    return old, win, loss


def test_distill_schema_watermark_and_counterexample():
    _reset_loop_state()
    _setup_book()
    new = IL.distill_cases(CFG)
    assert [c["id"] for c in new] == ["C-03", "C-04"]           # 基线前平仓不回填，编号接种子
    for c in new:
        assert set(c.keys()) == SEED_KEYS                        # 与种子同构
        assert set(c["outcome"].keys()) == {"text", "score_ids"}
        assert c["counter_example"]                              # 每条必带反例字段且非空
        assert c["status"] == "观察"
    win_case = next(c for c in new if "1308.13" in c["outcome"]["text"])
    assert win_case["regime"] == {"direction": "short", "strength": "strong",
                                  "stage": "late_exhaustion", "confidence": 0.6}
    assert win_case["session"] == "morning"
    assert win_case["outcome"]["score_ids"] == ["D-20261117-ETH:market"]
    assert "反例" in win_case["counter_example"] and "-52.58" in win_case["counter_example"]
    loss_case = next(c for c in new if "-52.58" in c["outcome"]["text"])
    assert "1308.13" in loss_case["counter_example"]             # 亏损案例的反例指向同币盈利笔
    assert IL.distill_cases(CFG) == []                           # 水位：不重复提炼
    assert len(settings.read_jsonl(IL.CASES)) == 4


def test_distill_no_counterexample_and_no_decision():
    _reset_loop_state()
    t = _trade("DEMO-ONLY", "SOL-USDT-SWAP", "D-MISSING",
               "2026-11-21 14:41:42+0800", "2026-11-21 20:00:00+0800", 67.22, 65.02, 698.21)
    settings.write_json(IL.CLOSED, {t["trade_id"]: t})
    new = IL.distill_cases(CFG)
    assert len(new) == 1
    assert new[0]["regime"] is None and new[0]["session"] == ""
    assert new[0]["counter_example"].startswith("无反例")


def _scores_file(rows):
    _write_jsonl(settings.STATE / "scores.jsonl", rows)


def _reset_loop_state():
    IL.LOOP_STATE.unlink(missing_ok=True)   # 会话级临时 STATE 跨用例共享，水位文件须显式复位


def _day(i, start="2026-11-14"):
    import datetime as _dt
    d = _dt.date.fromisoformat(start) + _dt.timedelta(days=i)
    return f"{d.isoformat()} 08:00:00+0800"


def _n_scores(role, regime, inst, n, hit=True):
    return [_score(role, regime, inst, _day(i), hit=(i % 3 != 2)) for i in range(n)]


def test_due_cells_threshold_and_watermark():
    _reset_loop_state()
    roles.ensure_playbooks()
    # 上批提案时间 2026-11-13 08:20：此前的评分不算新增
    settings.write_json(review.proposals_dir("market") / "last.json",
                        {"time": "2026-11-13 08:20:00+0800"})
    rows = (_n_scores("market", "下跌", "ETH-USDT-SWAP", 19)
            + [_score("market", "下跌", "ETH-USDT-SWAP", "2026-11-10 08:00:00+0800", True)])
    _scores_file(rows)
    assert IL.due_cells(CFG) == []                                # 19 条 < 门槛 20，且旧样本被水位挡住
    rows.append(_score("market", "下跌", "ETH-USDT-SWAP", "2026-12-03 08:00:00+0800", True))
    _scores_file(rows)
    due = IL.due_cells(CFG)
    assert len(due) == 1 and due[0]["role"] == "market" and due[0]["n"] == 20
    assert due[0]["regime"] == "下跌" and due[0]["inst_id"] == "ETH-USDT-SWAP"


class _FakeStructured:
    def __init__(self, ed):
        self._ed = ed

    def invoke(self, prompt):
        return self._ed


class _FakeLLM:
    def __init__(self, ed):
        self._ed = ed

    def with_structured_output(self, schema):
        return _FakeStructured(self._ed)


def _one_change_edit():
    return review.PlaybookEdit(no_change=False,
                               changes=[review.Change(op="新增", rule="规则一", why="依据统计")],
                               learned_rules="- 规则一")


def test_generate_batch_files_only_and_advances(monkeypatch):
    _reset_loop_state()
    roles.ensure_playbooks()
    settings.write_json(review.proposals_dir("news") / "last.json",
                        {"time": "2026-11-13 08:20:00+0800"})
    _scores_file(_n_scores("news", "下跌", "SOL-USDT-SWAP", 20))
    monkeypatch.setattr(IL.dllm, "make_llm", lambda cfg, tier, tracker, role=None: _FakeLLM(_one_change_edit()))
    before = roles.read_meta("news")["current"]
    events = IL.generate_batch(CFG, object())
    assert len(events) == 1 and events[0]["type"] == "建议" and events[0]["file"]
    f = review.proposals_dir("news") / events[0]["file"]
    assert f.exists() and "未生效" in f.read_text(encoding="utf-8")
    assert roles.read_meta("news")["current"] == before          # 只落盘：版本不切换
    assert settings.read_jsonl(IL.PB_LOG)[-1]["type"] == "建议"
    assert IL.due_cells(CFG) == []                               # 水位推进：同格不再达标


def test_generate_batch_rejects_over_limit(monkeypatch):
    _reset_loop_state()
    roles.ensure_playbooks()
    settings.write_json(review.proposals_dir("news") / "last.json",
                        {"time": "2026-11-13 08:20:00+0800"})
    _scores_file(_n_scores("news", "下跌", "SOL-USDT-SWAP", 20))
    ed = review.PlaybookEdit(no_change=False,
                             changes=[review.Change(op="新增", rule=f"规则{i}", why="依据")
                                      for i in range(3)],
                             learned_rules="- 规则")
    monkeypatch.setattr(IL.dllm, "make_llm", lambda cfg, tier, tracker, role=None: _FakeLLM(ed))
    events = IL.generate_batch(CFG, object())
    assert events[0]["type"] == "拒绝修订"                        # 每批 ≤2 处是硬护栏


def _chg(role, base, regime, inst, t0="2026-11-15 19:19:58+0800", version=2):
    return {"role": role, "type": "修订", "version": version, "time": t0,
            "baseline_hit": base, "env_scope": {"regime": regime, "inst_id": inst,
                                                "basis_n": 26, "mixed": False},
            "changes": [{"op": "新增", "rule": "r", "why": "w"}], "note": ""}


def test_trial_eval_verdicts_and_no_rollback():
    roles.ensure_playbooks()
    _write_jsonl(IL.PB_LOG, [_chg("market", 0.60, "下跌", "ETH-USDT-SWAP"),
                             _chg("news", 0.35, "下跌", "SOL-USDT-SWAP"),
                             _chg("flows", 0.35, "下跌", "ETH-USDT-SWAP")])
    rows = []
    # market：窗口 20 条中 10 中 → 0.50，比基线 0.60 低 10pp → 触发回滚线
    rows += [_score("market", "下跌", "ETH-USDT-SWAP", _day(i, "2026-11-16"),
                    hit=(i < 10), dec=f"D{i}") for i in range(20)]
    # market 的范围外样本（震荡）不得计入
    rows += [_score("market", "震荡", "ETH-USDT-SWAP", "2026-11-20 08:00:00+0800", hit=False, dec="DX")]
    # news：20 条中 12 中 → 0.60 > 基线 0.35 → 达标保留
    rows += [_score("news", "下跌", "SOL-USDT-SWAP", _day(i, "2026-11-16"),
                    hit=(i < 12), dec=f"D{i}") for i in range(20)]
    # flows：只有 7 条 → 样本不足
    rows += [_score("flows", "下跌", "ETH-USDT-SWAP", _day(i, "2026-11-16"),
                    hit=True, dec=f"D{i}") for i in range(7)]
    _scores_file(rows)
    meta_before = roles.read_meta("market")
    rep = IL.evaluate_trial(CFG)
    by = {r["role"]: r for r in rep["rows"]}
    assert by["market"]["post_hit"] == 0.5 and by["market"]["diff_pp"] == -10.0
    assert by["market"]["verdict"].startswith("触发回滚线")
    assert by["news"]["verdict"] == "达标保留"
    assert by["flows"]["verdict"].startswith("样本不足")
    assert "触发回滚线" in rep["text"]
    # 只算结论：手册版本与状态未被触碰（无回滚执行）——与评估前逐字比对（临时 PLAYBOOKS 跨用例共享）
    assert roles.read_meta("market") == meta_before


def test_run_tail_gated_by_switch():
    cfg = {"learning": {**CFG["learning"], "playbook_proposals": False}}
    cases_before = IL.CASES.read_bytes() if IL.CASES.exists() else None
    state_before = IL.LOOP_STATE.read_bytes() if IL.LOOP_STATE.exists() else None
    out = IL.run_tail(cfg, object())
    assert out == {"cases": [], "events": [], "trial": None}
    # 开关关闭时不写任何状态（与开关打开的用例共享同一临时 STATE，故比对前后字节）
    assert (IL.CASES.read_bytes() if IL.CASES.exists() else None) == cases_before
    assert (IL.LOOP_STATE.read_bytes() if IL.LOOP_STATE.exists() else None) == state_before
