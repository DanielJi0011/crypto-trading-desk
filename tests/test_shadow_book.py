"""反向影子账（2026-11-22 共识包 D 项）：事件识别（结构型/文本型闭集词表）、基线构造
几何、开仓/结算幂等、日报行。夹具按生产决策记录形状构造。"""
import datetime as _dt

import pandas as pd
import pytest

from desk import manager
from desk import settings
from desk import shadow_book as SB


def _rec(did="D-20261112-BTC", veto=True, rationale="现价追空赔率不足，RSI 超卖且有反弹风险，不加仓。",
         cs="short", price=100.0, atr=10.0):
    return {"decision_id": did, "date": "2026-11-12", "time": "2026-11-12 08:10:00+0800",
            "session": "morning", "inst_id": "BTC-USDT-SWAP", "consensus_side": cs,
            "tm_vetoed_consensus": veto, "tm_flip_blocked": False, "price": price,
            "market": {"atr14": atr, "ret_20d": -5.0},
            "manager": {"rationale": rationale},
            "stances": {"trade_manager": {"direction": "neutral"}}}


def test_identify_text_hit():
    ev = SB.identify(_rec())
    assert ev is not None and ev["shadow_side"] == "long" and ev["kind"] == "veto_counter_text"
    assert "反弹" in ev["markers"] and "超卖" in ev["markers"]


def test_identify_no_marker_no_event():
    assert SB.identify(_rec(rationale="证据不足，维持现有仓位不变，不新增入场单。")) is None


def test_identify_requires_veto():
    assert SB.identify(_rec(veto=False)) is None


def test_identify_long_consensus_mirror():
    ev = SB.identify(_rec(cs="long", rationale="上方冲高回落迹象明显，超买，不追多。"))
    assert ev is not None and ev["shadow_side"] == "short"


def test_identify_flip_blocked_structural():
    rec = _rec(veto=False)
    rec["tm_flip_blocked"] = True
    rec["stances"]["trade_manager"]["direction"] = "bullish"
    ev = SB.identify(rec)
    assert ev is not None and ev["kind"] == "flip_blocked" and ev["shadow_side"] == "long"


@pytest.fixture
def env(tmp_path, monkeypatch):
    led = tmp_path / "shadow_book.jsonl"
    dec = tmp_path / "decisions.jsonl"
    monkeypatch.setattr(SB, "LEDGER", led)
    monkeypatch.setattr(manager, "DECISIONS", dec)
    return led, dec


def _df_up(t0):
    n = 24 * 6
    dates = pd.date_range(t0, periods=n, freq="h")
    closes = [100.0 + i * 0.5 for i in range(n)]
    return pd.DataFrame({"Date": dates, "Open": closes,
                         "High": [c + 0.4 for c in closes], "Low": [c - 0.4 for c in closes],
                         "Close": closes})


def test_open_settle_idempotent(env):
    led, dec = env
    settings.append_jsonl(dec, _rec())
    from desk import gate_ledger
    t0 = gate_ledger._t0_utc("2026-11-12 08:10:00+0800")
    monkeypatch_df = _df_up(t0)
    SB.HOURLY = lambda inst: monkeypatch_df
    cfg = settings.load_config()
    now = _dt.datetime(2026, 11, 20, tzinfo=_dt.timezone.utc)
    out = SB.run_tail(cfg, now=now)
    assert out == {"opened": 1, "settled": 1}
    book = SB._merged()
    row = book["D-20261112-BTC"]
    # 影子多头：入场 100、止损 100−1.5×10=85、止盈 100+2×15=130；上行路径先触止盈 → +2R
    assert row["entry"] == 100.0 and row["sl"] == 85.0 and row["tp"] == 130.0
    assert row["status"] == "settled" and row["exit"] == "tp" and row["pnl_r"] == 2.0
    # 幂等：再跑不开新仓、不重结
    assert SB.run_tail(cfg, now=now) == {"opened": 0, "settled": 0}
    lines = SB.report_lines(cfg)
    assert len(lines) == 1 and "+2.00 R" in lines[0] and "赢 1" in lines[0]


def test_not_mature_stays_open(env):
    led, dec = env
    settings.append_jsonl(dec, _rec())
    cfg = settings.load_config()
    now = _dt.datetime(2026, 11, 13, tzinfo=_dt.timezone.utc)   # 决策后 1 天，未到期
    out = SB.run_tail(cfg, now=now)
    assert out == {"opened": 1, "settled": 0}
    assert SB._merged()["D-20261112-BTC"]["status"] == "open"
