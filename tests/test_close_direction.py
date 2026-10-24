"""平仓/减仓不等于反向观点：交易经理对已有持仓 sell/reduce 记为 direction="close"，不参与方向打分（全部离线）。"""
import datetime as dt

import pandas as pd
import pytest

from desk import manager, report, review, scoring, settings


def _dec(action, **kw):
    return manager.ManagerDecision(action=action, confidence=0.62, rationale="t", **kw)


@pytest.mark.parametrize("action", ["sell", "reduce"])
@pytest.mark.parametrize("side", ["short", "long"])
def test_close_or_reduce_is_close_not_directional(action, side):
    pos = {"side": side, "inst_id": "BTC-USDT-SWAP"}
    s = manager.tm_stance(_dec(action), {"last": 100}, pos)
    assert s["direction"] == "close"
    assert manager.tm_status(_dec(action), pos, {"last": 100}) == "go"      # 照常执行平仓/减仓，不算否决
    assert manager.tm_status(_dec(action), None, {"last": 100}) == "veto"   # 无持仓 sell/reduce 仍是否决
    assert report.DIR_CN["close"] == "平仓"


def test_open_actions_unchanged():
    assert manager.tm_stance(_dec("buy"), {"last": 100})["direction"] == "bullish"
    assert manager.tm_stance(_dec("short"), {"last": 100})["direction"] == "bearish"
    assert manager.tm_stance(_dec("hold"), {"last": 100})["direction"] == "neutral"


def test_close_contributes_zero_to_consensus_and_attribution():
    w = {r: 1.0 for r in manager.VOTERS}
    st = {"trade_manager": {"direction": "close", "confidence": 0.9},
          "market": {"direction": "bullish", "confidence": 0.5}}
    assert manager.consensus(st, w) == pytest.approx(0.25)
    att = scoring.pnl_attribution([{"status": "closed", "signal_id": "D1", "side": "sell", "pnl_usd": 10}], {"D1": st})
    assert att["trade_manager"] == 0
    assert review.role_call_table({"pnl_usd": 5, "side": "sell"}, None)  # 不抛异常


def test_close_stance_not_scored(monkeypatch):
    cfg = settings.load_config()
    for p in (scoring.STANCES, scoring.SCORES):
        if p.exists():
            p.unlink()
    base = {"decision_id": "D-X", "date": "2026-02-01", "time": "2026-02-01 08:00:00+0800", "inst_id": "BTC-USDT-SWAP",
            "confidence": 0.6, "target": None, "stop": None, "horizon_days": 5, "ref_px": 100.0, "regime": "震荡"}
    settings.append_jsonl(scoring.STANCES, {**base, "id": "D-X:trade_manager", "role": "trade_manager", "direction": "close"})
    settings.append_jsonl(scoring.STANCES, {**base, "id": "D-X:market", "role": "market", "direction": "bullish"})
    days = pd.date_range("2026-01-25", "2026-02-20", freq="D")
    df = pd.DataFrame({"Date": days, "Open": 100.0, "High": 112.0, "Low": 99.0, "Close": 110.0})
    monkeypatch.setattr(scoring, "DAILY", lambda inst: df)
    new = scoring.score_due(cfg, now=dt.datetime(2026, 3, 1, tzinfo=settings.TZ))
    assert [r["role"] for r in new] == ["market"]
    assert all(s["role"] != "trade_manager" for s in scoring.scores())
    for p in (scoring.STANCES, scoring.SCORES):
        p.unlink()
