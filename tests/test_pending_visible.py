"""挂着的条件单对所有角色可见（只读信息；读取失败降级）。"""
import pytest

from desk import analysis, demo_exec, manager, pending, rediscuss, roles, settings

TRIG = {"instId": "ETH-USDT-SWAP", "algoId": "A1", "side": "sell", "posSide": "short", "triggerPx": "2600", "sz": "12",
        "attachAlgoOrds": [{"slTriggerPx": "2680", "tpTriggerPx": "2400"}]}
BK_DEMO = {"cash": 1000.0, "equity": 1000.0, "exposure": 0.0, "positions": [], "triggers": [TRIG]}
VOTERS = ["market", "sentiment", "news", "flows", "bull", "bear", "research_manager", "trader",
          "aggressive", "conservative", "neutral", "portfolio_manager", "trade_manager"]


@pytest.fixture
def reg(monkeypatch):
    monkeypatch.setattr(demo_exec, "reg", lambda: {"triggers": {"A1": {"expires": "2026-10-26 08:00:00+0800"}}})


def test_with_pending(reg):
    b = analysis.pending_brief(BK_DEMO)
    assert "## 当前挂着的条件单" in b and "不要重复" in b
    for s in ("ETH-USDT-SWAP", "做空", "触发价 2600", "张数 12", "止损 2680", "止盈 2400", "到期 2026-10-26 08:00:00+0800"):
        assert s in b, s


def test_no_pending(monkeypatch):
    monkeypatch.setattr(demo_exec, "reg", lambda: {"triggers": {}})
    assert analysis.pending_brief({**BK_DEMO, "triggers": []}).endswith("- 无")
    monkeypatch.setattr(pending, "load", lambda: {})   # 纸面模式
    assert analysis.pending_brief({"positions": []}).endswith("- 无")


def test_read_failure(monkeypatch):
    def boom():
        raise RuntimeError("x")
    monkeypatch.setattr(pending, "load", boom)
    b = analysis.pending_brief({"positions": []})
    assert "挂单信息不可用" in b and "## 当前挂着的条件单" in b


def test_paper_pending(monkeypatch):
    monkeypatch.setattr(pending, "load", lambda: {"BTC-USDT-SWAP": {"side": "long", "trigger_px": 70000, "stop_loss": 68000,
                                                                      "take_profit": None, "expires": "E", "status": "pending"}})
    b = analysis.pending_brief({"positions": []})
    assert "BTC-USDT-SWAP：做多，触发价 70000" in b and "止损 68000" in b and "止盈 无" in b and "到期 E" in b


class _Inner:
    def invoke(self, msgs, config=None, **kw):
        return msgs


def _run_analyze(monkeypatch, session, extra=""):
    seen = {}

    class G:
        def propagate(self, *a, **k):
            for r in VOTERS:
                if r == "flows":
                    continue
                out = roles.PlaybookLLM(_Inner(), r).invoke("hi")
                seen[r] = "\n".join(str(m.content) for m in out)
            return {}, "HOLD"

        def save_reports(self, *a, **k):
            return None
    monkeypatch.setattr(settings, "SESSION", session)
    monkeypatch.setattr(analysis, "build_graph", lambda cfg, t: G())
    monkeypatch.setattr(analysis, "x_feed", lambda *a: "x")
    from desk import flows
    def fake_flows(*a, **k):
        out = roles.PlaybookLLM(_Inner(), "flows").invoke("hi")
        seen["flows"] = "\n".join(str(m.content) for m in out)
        return {"report": "r", "data": ""}
    monkeypatch.setattr(flows, "analyze", fake_flows)
    cfg = {"desk_analysts": ["flows"], "macro": {"enabled": False}}
    coin = {"inst_id": "ETH-USDT-SWAP", "ticker": "ETH-USD", "name": "ETH"}
    analysis.analyze(cfg, coin, "2026-10-25", BK_DEMO, None, extra_brief=extra)
    return seen


@pytest.mark.parametrize("session,extra", [("morning", ""), ("evening", ""),
                                           ("morning", "REDISCUSS")])
def test_every_role_every_flow(monkeypatch, reg, session, extra):
    if extra:
        extra = rediscuss.reason_brief("sl", "EVT")
    seen = _run_analyze(monkeypatch, session, extra)
    for r in VOTERS:
        assert "## 当前挂着的条件单" in seen[r] and "触发价 2600" in seen[r], r
        if extra:
            assert "再讨论" in seen[r], r


def test_manager_prompt(reg):
    cfg = {"guardrails": {"max_position_pct": 10, "max_total_notional_pct": 100, "max_stop_loss_pct": 8, "max_daily_loss_pct": 3},
           "manager": {}}
    mk = {"last": 2500.0, "close": 2500.0, "bar_date": "d", "ret_5d": 0, "ret_20d": 0, "atr14": 50.0, "hi_20": 1, "lo_20": 1}
    st = {r: {"direction": "hold"} for r in manager.ROLE_KEYS}
    texts = {"portfolio_manager": "", "trader": ""}
    p = manager._prompt(cfg, {"name": "ETH", "inst_id": "ETH-USDT-SWAP"}, mk, None, BK_DEMO, st, {}, 0.0, texts)
    assert "## 当前挂着的条件单" in p and "触发价 2600" in p and "到期 2026-10-26" in p
