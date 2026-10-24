"""S2：菜单外拒绝、NW t 值、每日一观测、全局计数、探索/确认、规则引用标注。无 LLM、隔离。"""
import numpy as np
import pandas as pd
import pytest

from desk import research, scoring, settings


@pytest.fixture
def iso(tmp_path, monkeypatch):
    for k, v in {"RDIR": tmp_path, "REGISTRY": tmp_path / "h.jsonl", "RESULTS": tmp_path / "r.jsonl",
                 "ANNOT": tmp_path / "a.jsonl", "REJECTED": tmp_path / "rej.jsonl"}.items():
        monkeypatch.setattr(research, k, v)
    return tmp_path


def test_nw_t_smaller_with_autocorr():
    rng = np.random.default_rng(1)
    e = rng.normal(0, 1, 400)
    x = 0.1 + np.convolve(e, np.ones(5), "same") / 5      # 重叠持有期式的正自相关
    assert research.nw_t(x, 5) < research._t(x)
    assert research.nw_t(x, 0) == pytest.approx(research._t(x), rel=0.02)
    assert research.nw_t([1, 1], 3) == 0.0


def test_per_day_merges_same_day():
    rows = [("2026-01-01 00:00", 0.1), ("2026-01-01T00:00", 0.3), ("2026-01-02", -0.1)]
    assert research.per_day(rows) == pytest.approx([0.2, -0.1])


def test_global_k_counts_all_roles(iso):
    for i, role in enumerate(["market", "news", "flows"]):
        settings.append_jsonl(research.RESULTS, {"id": f"H{i}", "role": role})
    settings.append_jsonl(research.RESULTS, {"id": "S", "skipped": True})
    assert research.global_k() == 4 and research.global_k(0) == 3


def test_propose_rejects_off_menu(iso, monkeypatch):
    cfg = settings.load_config()

    class H:
        def __init__(self, d): self.d = d
        def model_dump(self): return self.d

    good = {"name": "g", "side": "long", "conditions": [{"feature": "vol_z20", "op": ">", "value": 1}], "horizon_days": 3}
    bad = {"name": "b", "side": "long", "conditions": [{"feature": "vol_z20", "op": ">", "value": 1},
                                                       {"feature": "rsi_14", "op": "<", "value": 30}], "horizon_days": 3}

    class L:
        def with_structured_output(self, _): return self
        def invoke(self, _): return research.HypoList(hypotheses=[research.Hypo(rationale="x", **good),
                                                                 research.Hypo(rationale="x", **bad)])
    monkeypatch.setattr(research.dllm, "make_llm", lambda *a, **k: L())
    monkeypatch.setattr(research.roles, "current_text", lambda r: "")
    out = research.propose(cfg, "news", None, 3)
    assert [h["name"] for h in out] == ["g"]
    rej = settings.read_jsonl(research.REJECTED)
    assert len(rej) == 1 and "rsi_14" in rej[0]["why"]
    assert settings.read_jsonl(research.REGISTRY) == []       # 拒绝的不登记，不计数


def _data():
    rng = np.random.default_rng(0)
    n = 400
    t = pd.date_range("2024-01-01", periods=n, freq="D")
    out = {}
    for inst in ("BTC-USDT", "ETH-USDT"):
        c = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, n)))
        out[inst] = pd.DataFrame({"time": t, "o": c, "h": c * 1.01, "l": c * 0.99, "c": c, "v": rng.uniform(1, 2, n)})
    return out


def test_evaluate_explore_vs_confirm():
    cfg = settings.load_config()
    h = {"side": "long", "conditions": [{"feature": "ret_1", "op": ">", "value": -1}], "horizon_days": 3, "stop_pct": 8}
    data = _data()
    e = research.evaluate(h, data, {**cfg, "research": {**cfg["research"], "phase": "explore"}}, 50)
    c = research.evaluate(h, data, {**cfg, "research": {**cfg["research"], "phase": "confirm"}}, 50)
    assert e["passed"] is False and e["z_crit_is"] < c["z_crit_is"]
    assert "Newey-West" in e["t_method"]
    assert e["stats"]["is"]["n_days"] <= e["stats"]["is"]["n"]


def test_historical_annotations_present():
    import json, pathlib
    a = {}
    for l in (pathlib.Path(__file__).parent.parent / "research" / "annotations.jsonl").read_text().splitlines():
        a[json.loads(l)["id"]] = json.loads(l)
    assert len(a) == 8 and all(v["menu_violation"] for v in a.values())


def test_tag_rules():
    rec = scoring.tag_rules({}, {"reason": "依据 H-20261022-market-1bea23 与 H-20261022-flows-c09f54"})
    assert rec["rules_cited"] == ["H-20261022-flows-c09f54", "H-20261022-market-1bea23"]
    assert scoring.tag_rules({}, {"reason": None})["rules_cited"] == []
