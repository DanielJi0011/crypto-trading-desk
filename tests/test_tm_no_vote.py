"""2026-10-30 方案一：交易经理不投票、不计权重；入场点位只由交易经理定；盈亏记到交易经理名下。全部离线，下单函数全部打桩。"""
import json

import pytest

from desk import settings


def _dec(**kw):
    """交易经理下单工具的一次调用（固定走工具）。"""
    from desk.tm_order_tool import OrderToolCall
    base = dict(action="hold", rationale="t", invalidation="x")
    base.update(kw)
    return OrderToolCall(**base)


def _st(direction="bearish", entry_px=100.0, cond="now"):
    from desk import manager
    from desk.roles import ROLE_KEYS
    return {r: {"direction": direction, "confidence": 0.8, "stop": 104.0 if direction == "bearish" else 96.0,
                "target": 90.0 if direction == "bearish" else 110.0, "entry_px": entry_px, "entry_condition": cond,
                "horizon_days": 3} for r in ROLE_KEYS}


def _off(cfg):
    cfg["manager"].update(tm_no_vote_weight=False)
    cfg.setdefault("ledger", {})["tm_attribution"] = False
    return cfg


def test_config_defaults_true():
    cfg = settings.load_config()
    assert cfg["manager"]["tm_no_vote_weight"] is True
    assert cfg["ledger"]["tm_attribution"] is True


def test_weights_and_consensus_exclude_tm():
    from desk import manager
    cfg = settings.load_config()
    w = manager.weights(cfg)
    assert "trade_manager" not in w and "trade_manager" not in manager.voters(cfg)
    st = _st("bearish")
    st["trade_manager"] = {"direction": "bullish", "confidence": 1.0}
    # 交易经理即使看多满置信，也不改变共识
    assert manager.consensus(st, w) == pytest.approx(manager.consensus(_st("bearish"), w))
    assert "trade_manager" not in manager.valid_voters(st, w)
    # 开关关闭：交易经理重新作为一票
    w2 = manager.weights(_off(settings.load_config()))
    assert "trade_manager" in w2
    assert manager.consensus(st, w2) != pytest.approx(manager.consensus(_st("bearish"), w2))


def test_old_tm_weight_in_file_ignored(monkeypatch):
    from desk import manager, scoring
    settings.write_json(scoring.WEIGHTS, {"trade_manager": 1.9, "market_analyst": 1.0})
    try:
        cfg = settings.load_config()
        assert "trade_manager" not in manager.weights(cfg)
    finally:
        scoring.WEIGHTS.unlink()


def test_weight_learning_excludes_tm():
    from desk import scoring
    cfg = settings.load_config()
    assert "trade_manager" not in scoring.weight_roles(cfg)
    assert "trade_manager" in scoring.weight_roles(_off(settings.load_config()))
    settings.write_json(scoring.WEIGHTS_SHADOW, {"trade_manager": 1.3})   # 影子权重文件里旧的 trade_manager 记录
    try:
        scoring.update_weights(cfg)
        assert "trade_manager" not in json.loads(scoring.WEIGHTS_SHADOW.read_text())
    finally:
        for p in (scoring.WEIGHTS_SHADOW, scoring.WEIGHT_HIST):
            if p.exists():
                p.unlink()


@pytest.fixture()
def desk_env(monkeypatch):
    from desk import demo_exec as DX, manager, roles
    from desk import llm as dllm
    calls = {"open_now": [], "place_trigger": [], "cancel": []}
    monkeypatch.setattr(DX, "open_now", lambda *a, **k: calls["open_now"].append((a, k)) or (True, {}))
    monkeypatch.setattr(DX, "place_trigger", lambda *a, **k: calls["place_trigger"].append((a, k)) or (True, {"algoId": "x", "sz": "1"}))
    monkeypatch.setattr(DX, "cancel_triggers", lambda cfg, inst, why: calls["cancel"].append(why) or [])
    bk = {"equity": 100000.0, "live_equity": 100000.0, "initial": 100000.0, "cash": 100000.0, "exposure": 0.0, "positions": [], "triggers": [], "pending_entries": []}
    monkeypatch.setattr(DX, "is_demo", lambda cfg: True)
    monkeypatch.setattr(DX, "book", lambda cfg, prices=None: bk)
    monkeypatch.setattr(manager, "market_ctx", lambda inst: dict(MK))
    monkeypatch.setattr(manager, "role_texts", lambda fs: {"portfolio_manager": "pm", "trader": "t"})
    box = {"dec": None}

    class Fake:
        def with_structured_output(self, *_):
            return self

        def invoke(self, *_a, **_k):
            return box["dec"]
    monkeypatch.setattr(dllm, "make_llm", lambda *a, **k: Fake())
    monkeypatch.setattr(roles, "PlaybookLLM", lambda m, role: m)
    return manager, calls, box


MK = {"last": 100.0, "close": 100.0, "bar_date": "2026-10-29", "ret_5d": 0.0, "ret_20d": 0.0, "atr14": 2.0, "hi_20": 110.0, "lo_20": 90.0}

SOURCES = ["daily", "evening", "post_stop:T1", "post_tp:T1", "expiry:T1"]


@pytest.mark.parametrize("source", SOURCES)
def test_flip_rejected_all_paths(desk_env, source):
    """共识看空、交易经理想做多：工具校验退回 [DIRECTION]，3 次仍反向 → 无结论，不下单。"""
    manager, calls, box = desk_env
    cfg = settings.load_config()
    box["dec"] = _dec(action="open", side="long", entry=100.0, stop_loss=96.0, take_profit=110.0)
    rec = manager.decide_and_execute(cfg, cfg["coins"][0], "2026-10-30", {}, _st("bearish"), None, source=source)
    assert rec["tm_status"] in ("absent", "veto")
    assert "DIRECTION" in (rec.get("tm_absent_reason") or "")
    assert not calls["open_now"] and not calls["place_trigger"]
    assert rec["consensus_side"] == "short" and "trade_manager" not in rec["weights"]


def test_veto_and_open(desk_env):
    manager, calls, box = desk_env
    cfg = settings.load_config()
    box["dec"] = _dec()     # hold
    rec = manager.decide_and_execute(cfg, cfg["coins"][0], "2026-10-30", {}, _st("bearish"), None)
    assert rec["tm_status"] == "veto", rec["manager"]["rationale"]
    assert rec["tm_vetoed_consensus"] and rec["decision_type"] == "持有"
    assert not calls["open_now"] and not calls["place_trigger"]


def test_prompt_text(desk_env):
    from desk import manager
    cfg = settings.load_config()
    p = manager._prompt(cfg, cfg["coins"][0], dict(MK), None,
                        {"equity": 1e5, "initial": 1e5, "cash": 1e5, "exposure": 0.0, "positions": [], "triggers": [], "pending_entries": []}, _st(), manager.weights(cfg), -0.5, {"portfolio_manager": "pm", "trader": "t"}, None)
    p = p if isinstance(p, str) else str(p)
    assert "你不投票，权重不计" in p and "最终定点位" in p and "作为一票" not in p and "投票角色之一" not in p
    mine = p.split("你的角色：")[1].split("\n")[0]
    assert "—" not in mine and "--" not in mine


def _trade(tid="T-1", pnl=50.0, side="sell", did="D-X", **kw):
    t = {"trade_id": tid, "inst_id": "BTC-USDT-SWAP", "side": side, "signal_id": did, "status": "closed",
         "entry_px": 100.0, "exit_px": 95.0 if pnl > 0 else 102.0, "sl_px": 104.0, "tp_px": 90.0,
         "entry_time": "2026-10-28 08:00:00", "exit_time": "2026-10-30 08:00:00", "pnl_usd": pnl, "pnl_pct": pnl / 100,
         "exit_reason": "tp"}
    t.update(kw)
    return t


@pytest.fixture()
def ledger_clean():
    from desk import tm_ledger
    for p in (tm_ledger.PATH, tm_ledger.DECISIONS, tm_ledger.SCORES):
        if p.exists():
            p.unlink()
    yield tm_ledger
    for p in (tm_ledger.PATH, tm_ledger.DECISIONS, tm_ledger.SCORES):
        if p.exists():
            p.unlink()


def test_ledger_idempotent_and_fields(ledger_clean):
    L = ledger_clean
    cfg = settings.load_config()
    settings.append_jsonl(L.DECISIONS, {"decision_id": "D-V", "inst_id": "BTC-USDT-SWAP", "time": "2026-10-29 08:00:00",
                                        "tm_vetoed_consensus": True, "consensus_side": "short"})
    L.record_closed(cfg, [_trade("T-1", 50.0), _trade("T-2", -20.0)])
    L.record_closed(cfg, [_trade("T-1", 50.0), _trade("T-2", -20.0)])
    rows = L.read()
    assert [r["trade_id"] for r in rows] == ["T-1", "T-2"]
    r1 = rows[0]
    assert r1["realized_rr"] == pytest.approx(1.25) and r1["hold_days"] == pytest.approx(2.0)
    assert r1["open_decision_id"] == "D-X" and r1["vetoed_consensus"] is True and r1["decided_by"] == "trade_manager"
    s = L.stats()
    assert s["n"] == 2 and s["pnl"] == pytest.approx(30.0) and s["win_rate"] == pytest.approx(0.5)
    assert L.record_closed(_off(settings.load_config()), [_trade("T-3")]) == []


def test_veto_stats(ledger_clean):
    L = ledger_clean
    decs = [{"decision_id": "D1", "tm_vetoed_consensus": True, "consensus_side": "short"},
            {"decision_id": "D2", "tm_vetoed_consensus": True, "consensus_side": "short"},
            {"decision_id": "D3", "tm_vetoed_consensus": True, "consensus_side": "long"}]
    sc = [{"decision_id": "D1", "role": "trade_manager", "outcome": "平"},
          {"decision_id": "D2", "role": "market_analyst", "outcome": "跌"}]
    v = L.veto_stats(decs, sc)
    assert v == {"n": 3, "judged": 2, "veto_right": 1, "veto_wrong": 1}


def test_report_section_and_review(ledger_clean, monkeypatch):
    from desk import demo_exec as DX, report, review
    L = ledger_clean
    monkeypatch.setattr(DX, "is_demo", lambda cfg: False)
    cfg = settings.load_config()
    usage = {"calls": 0, "errors": 0, "input": 0, "cached": 0, "output": 0, "cost_usd": 0, "by_stage": {}, "by_model": {}, "by_provider": {}}
    txt = open(report.write(cfg, "2099-01-01", [], [], [], [], [], usage, {}, [], name="t-tm0"), encoding="utf-8").read()
    assert "交易经理战绩" in txt and "暂无数据" in txt
    assert "| 交易经理 | 不计（不投票）" in txt
    L.record_closed(cfg, [_trade("T-9", 50.0)])
    tr = _trade("T-9", 50.0)
    rv = {"trade_id": "T-9", "trade": tr, "table": review.role_call_table(tr, None), "summary": "s", "verdicts": [],
          "manager_lesson": "", "tm_line": review.tm_line(tr, None)}
    txt = open(report.write(cfg, "2099-01-01", [], [tr], [rv], [], [], usage, {}, [], name="t-tm1"), encoding="utf-8").read()
    sec = txt.split("交易经理战绩")[1].split("## 四")[0]
    assert "+50.00" in sec and "100.0%" in sec and "笔数：1" in sec
    assert "主线（交易经理）" in txt and "只作参考" in txt
    off = _off(settings.load_config())
    txt = open(report.write(off, "2099-01-01", [], [], [], [], [], usage, {}, [], name="t-tm2"), encoding="utf-8").read()
    assert "交易经理战绩" not in txt


def test_review_trade_tm_line(ledger_clean, monkeypatch):
    from desk import review
    from desk import llm as dllm
    seen = {}

    class Fake:
        def with_structured_output(self, *_):
            return self

        def invoke(self, p):
            seen["p"] = p
            return {"summary": "ok", "verdicts": [], "manager_lesson": "m"}
    monkeypatch.setattr(dllm, "make_llm", lambda *a, **k: Fake())
    cfg = settings.load_config()
    out = review.review_trade(cfg, _trade("T-R", 50.0), None)
    assert out["tm_line"]["realized_rr"] == pytest.approx(1.25) and "复盘主线：交易经理" in seen["p"]
    out = review.review_trade(_off(settings.load_config()), _trade("T-R2", 50.0), None)
    assert "tm_line" not in out and "复盘主线" not in seen["p"]
