"""L5：交易经理读手册、risk_scale 只降不升、risk_scale<=0 否决。"""
from desk import manager, roles, sizing, settings
from desk.manager import ManagerDecision


def test_playbook_injected(monkeypatch):
    seen = {}

    class Inner:
        def invoke(self, msgs, config=None, **kw):
            seen["msgs"] = msgs
            return "ok"
    monkeypatch.setattr(roles, "current_text", lambda r: "TM手册正文" if r == "trade_manager" else "")
    roles.PlaybookLLM(Inner(), "trade_manager").invoke("提示词")
    assert any("TM手册正文" in str(getattr(m, "content", m)) for m in seen["msgs"])


def test_tm_wrapped_in_playbook():
    src = open(manager.__file__, encoding="utf-8").read()
    assert 'roles.PlaybookLLM(dllm.make_llm(cfg, "deep", tracker, role="trade_manager")' in src


def test_repo_playbook_v004():
    import json, pathlib
    m = json.loads((pathlib.Path(__file__).parents[1] / "playbooks/trade_manager/meta.json").read_text(encoding="utf-8"))
    assert m["current"] == 4


def test_risk_scale_veto_and_default():
    d = ManagerDecision(action="buy", confidence=0.7, rationale="x", risk_scale=0)
    assert manager.tm_status(d, None, {"last": 100}) == "veto"
    d = ManagerDecision(action="buy", confidence=0.7, rationale="x")
    assert d.risk_scale == 1.0 and manager.tm_status(d, None, {"last": 100}) == "go"


def test_risk_scale_only_down():
    rc = sizing.rcfg(settings.load_config())
    kw = dict(equity=10000, S=0.6, entry=100, stop=97, side="long", full_size=0.6, half_below=0.3, lev_cap=1, mmr=0.005, atr=1)
    full = sizing.size(rc, **kw, tm_risk_scale=1.0)["risk_usd"]
    assert abs(sizing.size(rc, **kw, tm_risk_scale=0.5)["risk_usd"] - full / 2) < 1e-6
    assert abs(sizing.size(rc, **kw, tm_risk_scale=3.0)["risk_usd"] - full) < 1e-6
