"""L1：TradingAgents 内置记忆关闭、决策追溯字段、修订建议只落盘、日报展示影子权重。离线，不调用真实 LLM。"""
import json

from desk import analysis, review, roles, settings


class _G:
    def _memory_step(self, state):
        raise AssertionError("不应调用原始记忆步骤")


def test_memory_off_by_default():
    cfg = settings.load_config()
    assert cfg["learning"]["ta_memory_inject"] is False
    f = analysis.patch_ta_memory(cfg, _G)
    out = _G()._memory_step({"company_of_interest": "BTC-USD", "trade_date": "2026-10-23"})
    assert out["past_context"] == "" and f is analysis._memory_off


def test_memory_on_restores_original():
    cfg = settings.load_config()
    class G2:
        def _memory_step(self, state):
            return {"past_context": "x", "memory_note": ""}
    analysis._ORIG_MEMORY_STEP = None
    analysis.patch_ta_memory(cfg, G2)
    cfg["learning"]["ta_memory_inject"] = True
    analysis.patch_ta_memory(cfg, G2)
    assert G2()._memory_step({})["past_context"] == "x"
    analysis._ORIG_MEMORY_STEP = None


def test_real_vendor_class_patched():
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    orig = TradingAgentsGraph._memory_step
    try:
        analysis._ORIG_MEMORY_STEP = None
        analysis.patch_ta_memory(settings.load_config(), TradingAgentsGraph)
        assert TradingAgentsGraph._memory_step is analysis._memory_off
    finally:
        TradingAgentsGraph._memory_step = orig
        analysis._ORIG_MEMORY_STEP = None


def test_config_hash_stable_and_sensitive():
    cfg = settings.load_config()
    h = settings.config_hash(cfg)
    assert len(h) == 12 and h == settings.config_hash(settings.load_config())
    cfg["manager"]["consensus_threshold"] = 0.2
    assert settings.config_hash(cfg) != h


def test_versions_include_tm():
    roles.ensure_playbooks()
    v = roles.versions()
    assert "trade_manager" in v and set(roles.ROLE_KEYS) <= set(v)


def test_proposal_mode_writes_only_proposals(monkeypatch):
    roles.ensure_playbooks()
    cfg = settings.load_config()
    cfg["learning"]["playbook_proposals"] = True
    r = "market"
    before = roles.meta_path(r).read_text()
    meta = {"current": 1, "history": [{"version": 1, "time": "2026-01-01 00:00:00+0800", "eval_status": "baseline"}]}
    monkeypatch.setattr(review.roles, "read_meta", lambda role: meta)
    monkeypatch.setattr(review.roles, "save_new_version", lambda *a, **k: (_ for _ in ()).throw(AssertionError("建议模式不得切版本")))
    monkeypatch.setattr(review.roles, "current_text", lambda role: "手册")
    monkeypatch.setattr(review, "recent_lessons", lambda role: [])
    fresh = [{"hit": False, "date": "2026-10-01", "inst_id": "BTC", "regime": "震荡", "direction": "bullish",
              "confidence": 0.5, "outcome": "x", "ret_pct": -1.0, "stop_quality": "无"}] * 25
    monkeypatch.setattr(review, "ROLE_KEYS", [r])
    monkeypatch.setattr(review, "_since", lambda role, t0: fresh)
    monkeypatch.setattr(review.scoring, "scores", lambda role=None: fresh)
    monkeypatch.setattr(review.scoring, "scorecard", lambda role: {"n": 25, "recent": {"hit": 0.0}})
    ed = review.PlaybookEdit(no_change=False, learned_rules="- 新规则", changes=[review.Change(op="新增", rule="r", why="w")])

    class M:
        def with_structured_output(self, s):
            return self
        def invoke(self, p):
            return ed
    monkeypatch.setattr(review.dllm, "make_llm", lambda *a, **k: M())
    ev = review.maybe_edit_playbooks(cfg, None)
    assert ev[0]["type"] == "建议"
    assert roles.meta_path(r).read_text() == before                       # 版本未切换
    assert (review.proposals_dir(r) / ev[0]["file"]).exists()
    # 再跑一次：last.json 之后没有新打分 → _since 仍返回桩数据，但 t0 已含建议时间
    assert review._last_proposal(r)
