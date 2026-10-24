"""L4：输入数据缺失 → data_missing；x_feed 缺失每天只告警一次。"""
from desk import analysis, flows, settings, stances
from desk.stances import stance_status

FS = {"company_of_interest": "BTC", "market_report": "【立场】看多 目标 1 止损 1 置信 0.5 周期 3天",
      "sentiment_report": "【立场】看多 目标 1 止损 1 置信 0.5 周期 3天", "_flows_report": "【立场】看多 目标 1 止损 1 置信 0.5 周期 3天"}


def _extract(fs, monkeypatch):
    class Fake:
        def with_structured_output(self, *a):
            return self

        def invoke(self, *a):
            raise RuntimeError("no llm in tests")
    monkeypatch.setattr(stances.dllm, "make_llm", lambda *a, **k: Fake())
    return stances.extract(fs, settings.load_config())


def test_sentiment_missing_without_x_feed(monkeypatch):
    st = _extract({**FS, "_x_feed": False, "_flows_missing": False}, monkeypatch)
    assert stance_status(st["sentiment"]) == "data_missing"
    assert stance_status(st["flows"]) != "data_missing"


def test_flows_missing_flag(monkeypatch):
    st = _extract({**FS, "_x_feed": True, "_flows_missing": True}, monkeypatch)
    assert stance_status(st["flows"]) == "data_missing" and stance_status(st["sentiment"]) != "data_missing"


def test_old_path_untouched(monkeypatch):
    st = _extract(dict(FS), monkeypatch)
    assert stance_status(st["sentiment"]) != "data_missing" and stance_status(st["flows"]) != "data_missing"


def test_flows_error_ratio(monkeypatch):
    monkeypatch.setattr(flows, "fetch", lambda c: {"errors": ["a", "b", "c", "d"]})
    monkeypatch.setattr(flows, "brief", lambda c, d=None: "x")
    from desk import roles
    class M:
        def invoke(self, m):
            raise RuntimeError("no llm")
    monkeypatch.setattr(roles, "PlaybookLLM", lambda *a, **k: M())
    monkeypatch.setattr(flows.__dict__.get("dllm", analysis.dllm), "make_llm", lambda *a, **k: None)
    r = flows.analyze(settings.load_config(), "BTC")
    assert r["data_missing"] and r["fetch_errors"] == 4
    monkeypatch.setattr(flows, "fetch", lambda c: {"errors": ["a", "b", "c"]})
    assert not flows.analyze(settings.load_config(), "BTC")["data_missing"]


def test_x_feed_alert_once_per_day(monkeypatch):
    sent = []
    import desk.demo_ws as dw
    monkeypatch.setattr(dw, "notify", lambda ev: sent.append(ev) or True)
    assert analysis.x_feed_missing_alert("2026-10-23", "BTC")
    assert not analysis.x_feed_missing_alert("2026-10-23", "ETH")
    assert analysis.x_feed_missing_alert("2026-10-24", "BTC")
    assert [e["kind"] for e in sent] == ["x_feed_missing", "x_feed_missing"]
    assert "x_feed_missing" in dw.NOTIFY_KINDS
