import datetime as dt
import logging

import pytest

from desk import macro, settings


def _obs(sid):
    end = dt.date(2026, 10, 20)
    if sid == "CPIAUCSL":
        return [("2025-09-01", 300.0), ("2026-09-01", 309.0)]
    return [((end - dt.timedelta(days=d)).isoformat(), 4.0 + d / 100) for d in range(60, -1, -1)]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    if macro.CACHE.exists():
        macro.CACHE.unlink()
    monkeypatch.setenv("FRED_API_KEY", "SECRETKEY123")
    yield
    if macro.CACHE.exists():
        macro.CACHE.unlink()


def test_compute_and_brief(monkeypatch):
    calls = []
    monkeypatch.setattr(macro, "fetch_observations", lambda sid, k, s: calls.append(sid) or _obs(sid))
    m = macro.get({"macro": {"enabled": True}}, "2026-10-22")
    assert m["source"] == "fresh" and len(calls) == 7
    d = {i["id"]: i for i in m["items"]}
    assert d["DGS10"]["value"] == 4.0 and d["DGS10"]["chg_1w"] == pytest.approx(-0.07)
    assert d["DGS10"]["chg_1m"] == pytest.approx(-0.30)
    assert d["CPIAUCSL"]["yoy_pct"] == 3.0
    b = macro.brief(m)
    assert "宏观数据（FRED" in b and "同比 +3.00%" in b
    # 同日再次调用走缓存，不再请求
    m2 = macro.get({}, "2026-10-22")
    assert m2["source"] == "cache" and len(calls) == 7
    assert "| 10年美债收益率 | DGS10 | 4.00% | 2026-10-20 |" in "\n".join(macro.report_lines(m2))


def test_failure_uses_stale_cache_and_redacts(monkeypatch, caplog):
    monkeypatch.setattr(macro, "fetch_observations", lambda sid, k, s: _obs(sid))
    macro.get({}, "2026-10-21")

    def boom(sid, k, s):
        raise RuntimeError(macro.redact(f"HTTPError for url https://x/?series_id={sid}&api_key={k}&file_type=json"))
    monkeypatch.setattr(macro, "fetch_observations", boom)
    with caplog.at_level(logging.WARNING):
        m = macro.get({}, "2026-10-22")
    assert m["source"] == "stale_cache" and all(i.get("stale") for i in m["items"])
    assert "SECRETKEY123" not in caplog.text and "api_key=***" in caplog.text
    assert "旧缓存" in "\n".join(macro.report_lines(m))


def test_unavailable_no_cache(monkeypatch):
    def boom(*a):
        raise RuntimeError("timeout")
    monkeypatch.setattr(macro, "fetch_observations", boom)
    m = macro.safe_get({}, "2026-10-22")
    assert m["source"] == "unavailable"
    assert "不可用" in macro.brief(m)
    assert "SECRETKEY123" not in str(m)


def test_no_key_and_disabled(monkeypatch):
    monkeypatch.delenv("FRED_API_KEY")
    monkeypatch.setattr(macro, "fetch_observations", lambda *a: pytest.fail("不应请求"))
    assert macro.get({}, "2026-10-22")["source"] == "unavailable"
    m = macro.get({"macro": {"enabled": False}}, "2026-10-22")
    assert m["source"] == "disabled" and macro.brief(m) == "" and macro.report_lines(m) == []


def test_fetch_observations_mocked_requests(monkeypatch):
    import requests

    class R:
        def raise_for_status(self): pass
        def json(self): return {"observations": [{"date": "2026-10-19", "value": "."}, {"date": "2026-10-20", "value": "4.1"}]}
    seen = {}
    monkeypatch.setattr(requests, "get", lambda url, params, timeout: seen.update(url=url, p=params, t=timeout) or R())
    assert macro.fetch_observations("DGS10", "K", "2025-01-01") == [("2026-10-20", 4.1)]
    assert seen["t"] == 15 and seen["p"]["file_type"] == "json" and seen["url"] == macro.API


def test_research_features_unchanged():
    cfg = settings.load_config()
    assert cfg["research"]["role_features"]["news"] == ["vol_z20"]
    assert cfg["macro"]["enabled"] is True and len(cfg["macro"]["fred_series"]) == 7
