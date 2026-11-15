"""席位级续跑（desk/resume.py，2026-11-15）：存档点开关口径、组合指纹往返、
analyze 的 fresh 清理/续跑喂回/旁车落盘、尾段重建校验、resume_coin 三条路径，
以及真实图（模拟 LLM）跑到尾部节点抛错 → 存档点续跑只补尾段并完成。
全部离线（TA_DESK_MOCK_LLM=1 + 临时 TA_DESK_HOME），不碰真实 state/模拟盘。"""
from __future__ import annotations

import pytest

from desk import analysis, resume, settings
from desk import llm as dllm

DATE = "2026-10-19"
COIN = {"ticker": "BTC-USD", "inst_id": "BTC-USDT-SWAP", "name": "比特币"}


@pytest.fixture(autouse=True)
def _resume_off(monkeypatch):
    monkeypatch.setattr(settings, "RESUME_MODE", False, raising=False)
    yield


def _cfg():
    return settings.load_config()


# ------------------------------------------------------------------ 开关口径
def test_checkpoint_enabled_rules(monkeypatch):
    cfg = _cfg()
    assert cfg["parallel"]["enabled"] is True          # 仓库 config 现状：并行开（2026-10-28 用户同意）
    assert dllm.checkpoint_enabled(cfg) is False        # 定时场次（并行）：关，行为与启用前一致
    assert dllm.ta_config(cfg)["checkpoint_enabled"] is False
    monkeypatch.setattr(settings, "RESUME_MODE", True)
    assert dllm.checkpoint_enabled(cfg) is True         # 续跑入口（强制串行）：开
    assert dllm.ta_config(cfg)["checkpoint_enabled"] is True
    cfg2 = {**cfg, "parallel": {"enabled": False}}
    monkeypatch.setattr(settings, "RESUME_MODE", False)
    assert dllm.checkpoint_enabled(cfg2) is True        # 串行部署：开


def test_parallel_guard_still_raises(monkeypatch):
    # run_daily 并行守卫语义不变：并行 + ta_config 存档点开 → 明确报错而不是带病并行
    from desk import run_daily
    monkeypatch.setattr(settings, "RESUME_MODE", True)
    with pytest.raises(RuntimeError, match="checkpoint_enabled"):
        run_daily._run_coins_parallel(_cfg(), [], DATE, None, [], [])


# ------------------------------------------------------------------ 组合指纹往返（续跑命门）
def test_portfolio_fingerprint_roundtrip():
    from tradingagents.portfolio import PortfolioContext
    book = {"cash": 106313.94, "positions": [
        {"ticker": "ETH-USD", "qty": -42.27, "avg_px": 2024.02},
        {"ticker": "BTC-USD", "qty": 0.0, "avg_px": 0.0}]}
    pf = analysis.portfolio_ctx(book)
    pf2 = PortfolioContext.model_validate(pf.model_dump())
    assert pf2.fingerprint() == pf.fingerprint()
    assert pf2.model_dump_json() == pf.model_dump_json()


# ------------------------------------------------------------------ analyze（替身图）
class _StubGraph:
    """替身图：记录 propagate/clear 调用，propagate 返回一份最小 final_state。"""

    def __init__(self, cache_dir):
        self.config = {"checkpoint_enabled": True, "data_cache_dir": str(cache_dir)}
        self.propagate_calls = []
        self.clear_calls = []

    def _run_signature(self, asset_type, portfolio=None):
        return f"sig:{portfolio.fingerprint() if portfolio else 'none'}"

    def clear_checkpoint_on_success(self, ticker, date, asset_type="stock", portfolio=None):
        self.clear_calls.append(portfolio.fingerprint() if portfolio else None)

    def propagate(self, ticker, trade_date, asset_type="crypto", portfolio=None):
        self.propagate_calls.append(portfolio)
        fs = {"company_of_interest": ticker, "trade_date": trade_date,
              "market_report": "M", "sentiment_report": "S", "news_report": "N",
              "fundamentals_report": "", "investment_debate_state": {}, "risk_debate_state": {},
              "trader_investment_plan": "T", "investment_plan": "IP", "final_trade_decision": "FTD"}
        return fs, "Hold"

    def save_reports(self, fs, ticker, html=False):
        return None


@pytest.fixture
def stubbed(monkeypatch, tmp_path):
    cfg = _cfg()
    stub = _StubGraph(tmp_path / "cache")
    monkeypatch.setattr(analysis, "build_graph", lambda c, t: stub)
    from desk import flows
    monkeypatch.setattr(flows, "analyze", lambda c, ccy, tr: {"report": "FLOWREP", "data": ""})
    monkeypatch.setattr(settings, "RESUME_MODE", True)
    return cfg, stub


def _book(cash):
    return {"positions": [], "cash": cash, "triggers": []}


def test_analyze_fresh_clears_and_writes_sidecars(stubbed):
    cfg, stub = stubbed
    tracker = dllm.UsageTracker(cfg)
    res = analysis.analyze(cfg, COIN, DATE, _book(10000.0), tracker)
    assert res["resumed_from_checkpoint"] is False
    live_fp = analysis.portfolio_ctx(_book(10000.0)).fingerprint()
    assert stub.clear_calls == [live_fp]                       # fresh：先清本线程再跑
    ctx = resume.read_run_ctx("BTC-USD", DATE, "morning")
    assert ctx and ctx["portfolio"]["cash"] == 10000.0         # run-ctx 旁车落盘
    sc = settings.read_json(resume.tail_sidecar_path("BTC-USD", DATE, "morning"), None)
    assert sc and sc["_flows_report"] == "FLOWREP" and sc["signal"] == "Hold"
    assert sc["state_sha"] == resume.state_sha_of(res["final_state"])


def test_analyze_resume_feeds_back_original_portfolio(stubbed, monkeypatch):
    cfg, stub = stubbed
    tracker = dllm.UsageTracker(cfg)
    analysis.analyze(cfg, COIN, DATE, _book(10000.0), tracker)          # 第一跑：写下 run-ctx
    stub.clear_calls.clear(); stub.propagate_calls.clear()
    import tradingagents.graph.checkpointer as ck
    monkeypatch.setattr(ck, "checkpoint_step", lambda *a, **k: 7)       # 假装断点在第 7 步
    res = analysis.analyze(cfg, COIN, DATE, _book(8888.0), tracker, resume=True)   # 账本已变
    assert res["resumed_from_checkpoint"] is True
    assert stub.propagate_calls[-1].cash == 10000.0                     # 喂回原始组合，签名才对得上
    assert stub.clear_calls == []                                       # 续跑不清存档点


def test_analyze_parallel_process_writes_tail_sidecar_only(stubbed, monkeypatch):
    cfg, stub = stubbed
    monkeypatch.setattr(settings, "RESUME_MODE", False)                 # 并行定时场次口径
    tracker = dllm.UsageTracker(cfg)
    day = "2026-10-18"                                                  # 独立日期，避免与同文件其他用例共用旁车
    analysis.analyze(cfg, COIN, day, _book(10000.0), tracker)
    assert stub.clear_calls == []
    assert resume.read_run_ctx("BTC-USD", day, "morning") is None       # 不写 run-ctx
    sc = settings.read_json(resume.tail_sidecar_path("BTC-USD", day, "morning"), None)
    assert sc and sc["_flows_report"] == "FLOWREP"                      # tail 旁车照常写（尾段续跑靠它）


# ------------------------------------------------------------------ 尾段重建
def _full_fs():
    return {"company_of_interest": "BTC-USD", "trade_date": DATE,
            "market_report": "M", "sentiment_report": "S", "news_report": "N",
            "fundamentals_report": "F",
            "investment_debate_state": {"bull_history": "B", "bear_history": "BE",
                                        "history": "H", "current_response": "CR"},
            "trader_investment_plan": "T",
            "risk_debate_state": {"aggressive_history": "A", "conservative_history": "C",
                                  "neutral_history": "NE", "history": "RH"},
            "investment_plan": "IP", "final_trade_decision": "FTD",
            "_flows_report": "FLOWREP", "_x_feed": True, "_flows_missing": False}


def _seed_tail(session="morning", date=DATE):
    fs = _full_fs()
    fs["trade_date"] = date
    entry = {k: v for k, v in fs.items() if not k.startswith("_")}
    entry["final_rating"] = "Hold"; entry["run_settings"] = {}
    settings.write_json(resume.full_state_log_path("BTC-USD", date), entry)
    resume.write_tail_sidecar("BTC-USD", date, session, fs, "Hold")
    return entry


def test_load_tail_fs_ok_and_mismatch():
    _seed_tail()
    fs = resume.load_tail_fs(COIN, DATE, "morning")
    assert fs and fs["_flows_report"] == "FLOWREP" and fs["_signal"] == "Hold"
    assert fs["investment_debate_state"]["bull_history"] == "B"
    assert resume.load_tail_fs(COIN, DATE, "evening") is None           # 场次不符（旁车缺失）
    entry = settings.read_json(resume.full_state_log_path("BTC-USD", DATE), None)
    entry["market_report"] = "被另一场覆盖"
    settings.write_json(resume.full_state_log_path("BTC-USD", DATE), entry)
    assert resume.load_tail_fs(COIN, DATE, "morning") is None           # 哈希不一致：拒绝串场


def test_find_decision_matches_session():
    from desk import manager
    rows = [
        {"decision_id": "D-20261019-BTC", "date": DATE, "session": "morning", "inst_id": "BTC-USDT-SWAP"},
        {"decision_id": "D-20261019-BTC-9", "date": DATE, "session": "rediscuss", "inst_id": "BTC-USDT-SWAP"},
        {"decision_id": "D-20261019-E-BTC", "date": DATE, "session": "evening", "inst_id": "BTC-USDT-SWAP"},
    ]
    for r in rows:
        settings.append_jsonl(manager.DECISIONS, r)
    assert resume.find_decision(DATE, "morning", "BTC-USDT-SWAP")["decision_id"] == "D-20261019-BTC"
    assert resume.find_decision(DATE, "evening", "BTC-USDT-SWAP")["decision_id"] == "D-20261019-E-BTC"
    assert resume.find_decision("2026-10-20", "morning", "BTC-USDT-SWAP") is None


# ------------------------------------------------------------------ resume_coin 三条路径
def test_resume_coin_done_skips(monkeypatch):
    from desk import manager
    settings.append_jsonl(manager.DECISIONS,
                          {"decision_id": "D-1", "date": DATE, "session": "morning",
                           "inst_id": "BTC-USDT-SWAP", "consensus": 0.1, "final_action": "hold"})
    monkeypatch.setattr(analysis, "analyze", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应再跑图")))
    rec, how = resume.resume_coin(_cfg(), COIN, DATE, None)
    assert how == "done" and rec["decision_id"] == "D-1"


def test_resume_coin_tail(monkeypatch):
    day = "2026-10-17"                                                  # 独立日期：同文件其他用例已给 DATE 写过决策记录
    _seed_tail(date=day)
    seen = {}
    from desk import manager, stances
    monkeypatch.setattr(stances, "extract", lambda fs, cfg, tracker=None: {})
    def fake_decide(cfg, coin, date, fs, st, tracker, source="daily"):
        seen["fs"] = fs
        return {"decision_id": "D-2", "consensus": -0.2, "final_action": "hold"}
    monkeypatch.setattr(manager, "decide_and_execute", fake_decide)
    monkeypatch.setattr(analysis, "analyze", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应再跑图")))
    rec, how = resume.resume_coin(_cfg(), COIN, day, None)
    assert how == "tail" and seen["fs"]["_flows_report"] == "FLOWREP"


def test_resume_coin_graph_fresh(monkeypatch):
    from desk import demo_exec, drift, ledger, manager, okx_data, stances
    monkeypatch.setattr(okx_data, "last_price", lambda inst: 100.0)
    monkeypatch.setattr(demo_exec, "is_demo", lambda cfg: False)
    monkeypatch.setattr(ledger, "book", lambda cfg, prices: {"positions": [], "cash": 1.0})
    monkeypatch.setattr(drift, "stamp", lambda inst: {"px": 100.0, "time": "t"})
    monkeypatch.setattr(analysis, "analyze", lambda cfg, coin, date, bk, tracker, resume=False: {
        "final_state": {"company_of_interest": "BTC-USD"}, "signal": "Hold",
        "report_dir": None, "resumed_from_checkpoint": False})
    monkeypatch.setattr(stances, "extract", lambda fs, cfg, tracker=None: {})
    monkeypatch.setattr(manager, "decide_and_execute",
                        lambda cfg, coin, date, fs, st, tracker: {"decision_id": "D-3", "consensus": 0.0,
                                                                  "final_action": "hold"})
    rec, how = resume.resume_coin(_cfg(), COIN, "2026-10-16", None)     # 独立日期，同上
    assert how == "graph-fresh" and rec["decision_id"] == "D-3"


# ------------------------------------------------------------------ 真实图存档点（模拟 LLM）
def test_real_graph_checkpoint_resume(monkeypatch):
    """全量跑测基线 → 倒数第二次调用抛错（模拟 opus 在尾部节点断线）→ 存档点在 →
    换回健康模型后以 resume 补跑：调用数远小于全量，且完成后存档点被清。"""
    from tradingagents.graph.checkpointer import checkpoint_step
    from desk import flows
    monkeypatch.setattr(flows, "analyze", lambda c, ccy, tr: {"report": "F", "data": ""})
    monkeypatch.setattr(settings, "RESUME_MODE", True)
    monkeypatch.setattr(analysis, "_graph", None)
    cfg = _cfg()
    tracker = dllm.UsageTracker(cfg)
    book = {"positions": [], "cash": 10000.0, "triggers": []}
    orig = dllm.FakeChatModel._generate
    state = {"n": 0, "fail_at": None}

    def counting(self, messages, *a, **k):
        state["n"] += 1
        if state["fail_at"] is not None and state["n"] >= state["fail_at"]:
            raise RuntimeError("模拟线路中断（opus 503）")
        return orig(self, messages, *a, **k)
    monkeypatch.setattr(dllm.FakeChatModel, "_generate", counting)

    res = analysis.analyze(cfg, COIN, DATE, book, tracker)              # 基线全量跑
    full_calls = state["n"]
    assert res["final_state"] and full_calls > 10
    g = analysis._graph
    sig = g._run_signature("crypto", analysis.portfolio_ctx(book))
    cache = g.config["data_cache_dir"]
    assert checkpoint_step(cache, "BTC-USD", DATE, sig) is None          # 成功收尾已清存档点

    state["n"] = 0; state["fail_at"] = full_calls - 1                    # 第二跑：尾部断线
    with pytest.raises(RuntimeError, match="模拟线路中断"):
        analysis.analyze(cfg, COIN, DATE, book, tracker)
    step = checkpoint_step(cache, "BTC-USD", DATE, sig)
    assert step is not None                                              # 断点留下

    state["n"] = 0; state["fail_at"] = None                              # 线路恢复：续跑
    res2 = analysis.analyze(cfg, COIN, DATE, {**book, "cash": 7777.0}, tracker, resume=True)
    assert res2["resumed_from_checkpoint"] is True
    assert res2["final_state"]
    assert 0 < state["n"] < full_calls                                   # 只补了尾段，没整币重跑
    assert checkpoint_step(cache, "BTC-USD", DATE, sig) is None          # 完成后清存档点
