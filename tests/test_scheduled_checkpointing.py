"""定时场次断点续跑 Plan A（2026-11-21，用户批准）：定时场次改串行 + 开存档点，
图内崩溃后场内对该币续跑一次；日报如实记录每币路径。

生产形状：复现 2026-11-20 晚场 BTC 的崩溃点——图内中转工具调用错误
（langchain_openai OpenAIInvalidRequestError，400 invalid_request_error，
assistant tool_calls 后无对应 tool message），存档点线程键 ticker+日期+签名、
run-ctx 旁车字段分布与生产一致。2026-11-21 早晨的教训：夹具形状不对，
测试全绿、生产永不触发——本文件所有夹具都按生产实际的键分布搭。
全部离线（TA_DESK_MOCK_LLM=1 + 临时 TA_DESK_HOME），不碰真实 state/模拟盘。
"""
from __future__ import annotations

import httpx
import pytest
from langchain_openai.chat_models.base import OpenAIInvalidRequestError

from desk import analysis, resume, settings
from desk import llm as dllm
from desk.roles import ROLE_KEYS

DATE = "2026-11-20"          # 生产崩溃场次：2026-11-20 晚场
SESSION = "evening"
COIN = {"ticker": "BTC-USD", "inst_id": "BTC-USDT-SWAP", "name": "比特币"}
COINS3 = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]


def _tool_call_error():
    """生产 11-20 晚场 BTC 的真实报错形状：OpenAIInvalidRequestError（400）。"""
    return OpenAIInvalidRequestError(
        "An assistant message with 'tool_calls' must be followed by tool messages responding to each 'tool_call_id'. "
        "(insufficient tool messages following tool_calls message) (request_id: 4acb5127-7658-4831-af23-ff842c831477)",
        response=httpx.Response(400, request=httpx.Request("POST", "https://api-direct.xlive.qzz.io/v1/chat/completions")),
        body={"error": {"message": "An assistant message with 'tool_calls' must be followed by tool messages responding to each 'tool_call_id'.",
                        "type": "invalid_request_error", "param": None, "code": "invalid_request_error"}})


def _cfg_serial():
    cfg = settings.load_config()
    cfg = {**cfg, "parallel": {"enabled": False}}   # Plan A：定时场次串行 + 存档点开
    return cfg


def _book(cash=10000.0):
    return {"positions": [], "cash": cash, "triggers": [], "equity": cash, "initial": cash,
            "live_equity": cash, "exposure": 0.0, "equity_basis": "paper"}


# ------------------------------------------------------------------ 串行场次编排替身
@pytest.fixture
def serial_harness(monkeypatch):
    """run_daily.main 串行路径替身：重活打桩，只留编排逻辑（首次失败→场内续跑一次）。
    resume_coin 走真实逻辑；分析/决策用替身，崩溃注入点可控。"""
    import threading
    from desk import (baselines, demo_trades, ledger, macro, manager, okx_data, report, research, review,
                      scoring, stances)
    from desk import run_daily
    H = {"events": [], "lock": threading.Lock(), "analyze_calls": {}, "decide_paths": [],
         "report": None, "fail_once": set(), "fail_always": set()}
    real_load = settings.load_config

    def load():
        c = real_load()
        c["parallel"] = {"enabled": False}
        return c
    monkeypatch.setattr(settings, "load_config", load)
    monkeypatch.setattr(settings, "RESUME_MODE", False, raising=False)

    def ev(*x):
        with H["lock"]:
            H["events"].append(x)
    monkeypatch.setattr(okx_data, "last_price", lambda inst: 100.0)
    monkeypatch.setattr(ledger, "book", lambda cfg, prices=None: {"positions": [], "cash": 10000.0, "triggers": []})
    monkeypatch.setattr(analysis, "build_graph", lambda cfg, tr: None)

    def analyze(cfg, coin, date, bk, tracker, extra_brief="", **kw):
        inst = coin["inst_id"]
        H["analyze_calls"][inst] = H["analyze_calls"].get(inst, 0) + 1
        if inst in H["fail_always"]:
            raise _tool_call_error()
        if inst in H["fail_once"] and H["analyze_calls"][inst] == 1:
            raise _tool_call_error()
        return {"final_state": {"inst": inst}, "signal": "Hold", "report_dir": f"/r/{inst}",
                "resumed_from_checkpoint": bool(kw.get("resume"))}
    monkeypatch.setattr(analysis, "analyze", analyze)

    def extract(fs, cfg, tracker):
        return {r: {"direction": "neutral", "confidence": 0.5} for r in ROLE_KEYS}
    monkeypatch.setattr(stances, "extract", extract)

    def decide(cfg, coin, date, fs, st, tracker, source="daily", run_path="normal"):
        inst = coin["inst_id"]
        H["decide_paths"].append((inst, run_path))
        return {"inst_id": inst, "decision_id": f"D-x-{inst[:3]}", "consensus": 0.1, "final_action": "hold",
                "stances": st, "run_path": run_path}
    monkeypatch.setattr(manager, "decide_and_execute", decide)
    monkeypatch.setattr(scoring, "record_stances", lambda cfg, rec: ev("record", rec["inst_id"]))
    monkeypatch.setattr(scoring, "score_due", lambda cfg: None)
    monkeypatch.setattr(scoring, "update_weights", lambda cfg: [])
    monkeypatch.setattr(macro, "safe_get", lambda cfg, d: None)
    monkeypatch.setattr(demo_trades, "review_candidates", lambda *a, **k: [])
    monkeypatch.setattr(review, "evaluate_and_rollback", lambda cfg: [])
    monkeypatch.setattr(review, "maybe_edit_playbooks", lambda cfg, tr: [])
    monkeypatch.setattr(research, "run", lambda *a, **k: {"skipped": True})
    monkeypatch.setattr(baselines, "update", lambda *a, **k: None)

    def rep(cfg, date, decisions, closed, reviews, wc, pb, usage, prices, errors, **k):
        H["report"] = {"decisions": [dict(d) for d in decisions], "errors": list(errors)}
        return "/dev/null"
    monkeypatch.setattr(report, "write", rep)

    def run():
        H["events"].clear(); H["report"] = None
        rc = run_daily.main(["--mock", "--date", "2026-11-20", "--report-name", "ckpt-test"])
        return rc, H["report"]
    H["run"] = run
    return H


def test_serial_retry_graph_resume_once(serial_harness):
    """首次图内崩溃（11-20 晚场同款 OpenAIInvalidRequestError）→ 场内续跑 1 次成功：
    决策只落一条、路径 graph-resume、errors 为空、BTC 只多跑一次。"""
    H = serial_harness
    H["fail_once"] = {"BTC-USDT-SWAP"}
    rc, rep = H["run"]()
    assert rc == 0
    assert [d["inst_id"] for d in rep["decisions"]] == COINS3
    assert rep["errors"] == []
    assert H["analyze_calls"] == {"BTC-USDT-SWAP": 2, "ETH-USDT-SWAP": 1, "SOL-USDT-SWAP": 1}
    assert H["decide_paths"] == [("BTC-USDT-SWAP", "graph-resume"),   # 首次尝试死在分析里，未进决策
                                 ("ETH-USDT-SWAP", "normal"), ("SOL-USDT-SWAP", "normal")]
    assert [d["run_path"] for d in rep["decisions"]] == ["graph-resume", "normal", "normal"]
    assert [e[1] for e in H["events"] if e[0] == "record"] == COINS3   # 立场只记一次/币，不翻倍


def test_serial_retry_still_fails_records_error(serial_harness):
    """续跑也失败：该币无决策、errors 明确写出两次失败，BTC/SOL 不受影响；分析恰好跑两次（首次+续跑各一）。"""
    H = serial_harness
    H["fail_always"] = {"ETH-USDT-SWAP"}
    rc, rep = H["run"]()
    assert rc == 0
    assert [d["inst_id"] for d in rep["decisions"]] == ["BTC-USDT-SWAP", "SOL-USDT-SWAP"]
    assert H["analyze_calls"]["ETH-USDT-SWAP"] == 2
    assert len(rep["errors"]) == 1
    err = rep["errors"][0]
    assert "ETH-USDT-SWAP" in err and "场内断点续跑 1 次后仍失败" in err and "OpenAIInvalidRequestError" in err


def test_ensure_stances_done_branch_no_double():
    """done 分支立场补记：缺失时补一次，已有不重复。"""
    from desk import run_daily, scoring
    cfg = _cfg_serial()
    rec = {"decision_id": "D-ensure-1", "date": "2026-11-20", "session": "evening",
           "inst_id": "BTC-USDT-SWAP", "time": "2026-11-20 21:05:00+0800",
           "stances": {r: {"direction": "neutral", "confidence": 0.5} for r in ROLE_KEYS},
           "market": {"ret_20d": -1.0}, "price": 60000.0}
    run_daily._ensure_stances(cfg, rec)
    rows = [r for r in settings.read_jsonl(scoring.STANCES) if r.get("decision_id") == "D-ensure-1"]
    assert len(rows) == len(ROLE_KEYS)
    run_daily._ensure_stances(cfg, rec)
    rows = [r for r in settings.read_jsonl(scoring.STANCES) if r.get("decision_id") == "D-ensure-1"]
    assert len(rows) == len(ROLE_KEYS)   # 第二次不动，不翻倍


# ------------------------------------------------------------------ 真实图：11-20 晚场 BTC 崩溃复现
@pytest.fixture
def _real_graph_setup(monkeypatch):
    from desk import flows
    monkeypatch.setattr(flows, "analyze", lambda c, ccy, tr: {"report": "F", "data": ""})
    monkeypatch.setattr(settings, "RESUME_MODE", False, raising=False)
    monkeypatch.setattr(settings, "SESSION", SESSION)
    monkeypatch.setattr(analysis, "_graph", None)
    return _cfg_serial()


def _counting_generate(monkeypatch, fail_at):
    """FakeChatModel._generate 计数包装：第 fail_at 次调用抛生产同款错；fail_at=None 不抛。"""
    orig = dllm.FakeChatModel._generate
    state = {"n": 0, "fail_at": fail_at}

    def counting(self, messages, *a, **k):
        state["n"] += 1
        if state["fail_at"] is not None and state["n"] >= state["fail_at"]:
            raise _tool_call_error()
        return orig(self, messages, *a, **k)
    monkeypatch.setattr(dllm.FakeChatModel, "_generate", counting)
    return state


def _stub_decide_parts(monkeypatch):
    """真实 decide_and_execute 的重活打桩（与 test_drift_check 同款口径）：
    行情快照用 11-20 真实极值（已收盘 hi 81199.9 / lo 57695.3），执行只返回 hold，不下单。"""
    from desk import demo_exec, ledger, manager, okx_data
    monkeypatch.setattr(okx_data, "last_price", lambda inst: 58919.5)
    monkeypatch.setattr(demo_exec, "book", lambda cfg, prices=None: _book(10000.0))
    monkeypatch.setattr(ledger, "book", lambda cfg, prices=None: _book(10000.0))
    monkeypatch.setattr(manager, "market_ctx", lambda inst: {"last": 58919.5, "ret_20d": -20.0, "ret_5d": -5.0,
                                                             "atr14": 2500.0, "close": 58919.5, "bar_date": "2026-11-20",
                                                             "hi_20": 81199.9, "lo_20": 57695.3})
    monkeypatch.setattr(manager, "add_preview", lambda *a, **k: None)
    monkeypatch.setattr(manager, "role_texts", lambda fs: {})

    def fake_pm(cfg, coin, did, st, w, mk, bk, pos, rationale, source="daily", tm=None, tm_state=None):
        return {"action": "hold", "size_pct": 0.0, "notes": [], "execution": None,
                "aggregate": {"S": 0.0}, "pending": None}
    monkeypatch.setattr(manager, "pm_execute", fake_pm)


def test_real_graph_1120_btc_crash_resumes_once(_real_graph_setup, monkeypatch):
    """11-20 晚场 BTC 生产形状复现：真实图（模拟 LLM）第 10 次调用抛
    OpenAIInvalidRequestError（生产：第 9 次调用后断线）→ 存档点留下、决策记录为零；
    换回健康模型后 resume_coin 从存档点续跑：路径 graph-resume、决策记录恰好一条、
    补跑调用数小于全量。"""
    from tradingagents.graph.checkpointer import checkpoint_step
    from desk import manager, stances
    cfg = _real_graph_setup
    tracker = dllm.UsageTracker(cfg)
    book = _book(10000.0)

    # 基线全量（另一日期，避免尾段旁车干扰 11-20 的续跑路径判定）
    state = _counting_generate(monkeypatch, None)
    res0 = analysis.analyze(cfg, COIN, "2026-11-19", book, tracker)
    full_calls = state["n"]
    assert res0["final_state"] and full_calls > 10

    # 11-20 晚场：跑到第 10 次调用断线（生产形状）
    _stub_decide_parts(monkeypatch)
    analyze_calls = {}

    def extract(fs, cfg, tracker=None):
        analyze_calls["n"] = state["n"]   # 立场抽取在分析之后、决策之前：此刻计数 = 本轮分析阶段调用数
        return {r: {"direction": "neutral", "confidence": 0.5} for r in ROLE_KEYS}
    monkeypatch.setattr(stances, "extract", extract)
    state["n"] = 0; state["fail_at"] = 10
    with pytest.raises(Exception, match="tool_calls"):
        resume.resume_coin(cfg, COIN, DATE, tracker)
    g = analysis._graph
    sig = g._run_signature("crypto", analysis.portfolio_ctx(book))
    cache = g.config["data_cache_dir"]
    assert checkpoint_step(cache, "BTC-USD", DATE, sig) is not None   # 断点留下
    assert resume.find_decision(DATE, SESSION, "BTC-USDT-SWAP") is None

    # 线路恢复：续跑
    state["n"] = 0; state["fail_at"] = None
    rec, how = resume.resume_coin(cfg, COIN, DATE, tracker)
    assert how == "graph-resume" and rec["run_path"] == "graph-resume"
    assert 0 < analyze_calls["n"] < full_calls                        # 只补了剩余节点，没整币重跑
    assert checkpoint_step(cache, "BTC-USD", DATE, sig) is None        # 完成后清存档点
    rows = [r for r in settings.read_jsonl(manager.DECISIONS)
            if r.get("date") == DATE and r.get("session") == SESSION and r.get("inst_id") == "BTC-USDT-SWAP"]
    assert len(rows) == 1 and rows[0]["run_path"] == "graph-resume"    # 决策记录恰好一条
    # 第三次补跑应直接跳过（done），不产生第二条记录
    rec2, how2 = resume.resume_coin(cfg, COIN, DATE, tracker)
    assert how2 == "done"
    rows = [r for r in settings.read_jsonl(manager.DECISIONS)
            if r.get("date") == DATE and r.get("session") == SESSION and r.get("inst_id") == "BTC-USDT-SWAP"]
    assert len(rows) == 1


# ------------------------------------------------------------------ 决策记录与日报字段
def test_decide_persists_run_path_and_report_renders_it(monkeypatch, tmp_path):
    """真实 decide_and_execute 落盘 run_path；日报对非正常路径写一行、本场路径字段。"""
    from desk import demo_exec, manager, okx_data, report
    from desk import equity_guard as EG, okx_demo as X
    monkeypatch.setattr(X, "_inst_cache", {"BTC-USDT-SWAP": {"ctVal": "0.01", "lotSz": "0.01", "minSz": "0.01", "tickSz": "0.1"}})
    monkeypatch.setattr(X, "EVENTS", tmp_path / "ev.jsonl")
    monkeypatch.setattr(EG, "MARKS", tmp_path / "m.json")
    monkeypatch.setattr(demo_exec, "REG", tmp_path / "reg.json")
    monkeypatch.setattr(demo_exec, "today_realized", lambda: 0.0)
    monkeypatch.setattr(demo_exec, "today_orders", lambda: 0)
    cfg = _cfg_serial()
    _stub_decide_parts(monkeypatch)
    from desk import stances
    monkeypatch.setattr(stances, "extract", lambda fs, cfg, tracker=None: {})
    monkeypatch.setattr(manager, "DECISIONS", tmp_path / "dec.jsonl")
    coin = next(c for c in cfg["coins"] if c["inst_id"] == "BTC-USDT-SWAP")
    st = {r: {"direction": "neutral", "confidence": 0.5, "source": "t"} for r in ROLE_KEYS}
    rec = manager.decide_and_execute(cfg, coin, DATE, {"_analysis_px": {"px": 58919.5, "time": "2026-11-20 21:00:05"}},
                                     st, None, run_path="graph-resume")
    assert rec["run_path"] == "graph-resume"
    saved = settings.read_jsonl(tmp_path / "dec.jsonl")[-1]
    assert saved["run_path"] == "graph-resume" and saved["decision_id"] == rec["decision_id"]
    usage = {"calls": 0, "errors": 0, "input": 0, "cached": 0, "output": 0, "cost_usd": 0, "by_stage": {}, "by_model": {}, "by_provider": {}}
    p = report.write(cfg, DATE, [rec], [], [], [], [], usage, {}, [], name="t-ckpt")
    txt = open(p, encoding="utf-8").read()
    assert "本场路径：图续跑（图内崩溃后从存档点续跑 1 次）" in txt
    rec2 = dict(rec, run_path="normal")
    p2 = report.write(cfg, DATE, [rec2], [], [], [], [], usage, {}, [], name="t-ckpt2")
    assert "本场路径" not in open(p2, encoding="utf-8").read()
