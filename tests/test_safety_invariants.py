"""整改任务书第 1 节规则 2：模拟盘安全不变量必须始终成立（每组整改都跑）。"""
import pytest

from desk import ledger, okx_demo as X, demo_ws as W


def test_assert_paper_still_blocks_live():
    with pytest.raises(ledger.LiveTradingDisabled):
        ledger.assert_paper({"mode": "live"})


def test_simulated_header_forced(monkeypatch):
    seen = {}

    class R:
        status_code = 200
        def json(self):
            return {"code": "0", "data": []}

    def fake(method, url, headers=None, data=None, timeout=None):
        seen.update(headers=headers, url=url); return R()
    monkeypatch.setenv("OKX_DEMO_API_KEY", "k"); monkeypatch.setenv("OKX_DEMO_SECRET_KEY", "s"); monkeypatch.setenv("OKX_DEMO_PASSPHRASE", "p")
    monkeypatch.setattr(X.requests, "request", fake)
    X.get("/api/v5/account/config")
    assert seen["headers"]["x-simulated-trading"] == "1"


def test_ws_urls_demo_only():
    assert W.URLS and all("wspap.okx.com" in u for u in W.URLS.values())


def test_keys_never_fall_back_to_live(monkeypatch):
    for k in X.KEY_ENVS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OKX_API_KEY", "live"); monkeypatch.setenv("OKX_API_SECRET", "live"); monkeypatch.setenv("OKX_API_PASSPHRASE", "live")
    assert X.KEY_ENVS == ("OKX_DEMO_API_KEY", "OKX_DEMO_SECRET_KEY", "OKX_DEMO_PASSPHRASE")
    with pytest.raises(X.DemoError):
        X._keys()
