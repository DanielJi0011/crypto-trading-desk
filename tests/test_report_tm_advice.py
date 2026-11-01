"""第 3 点：日报“交易经理建议”行按 action 显示，缺字段不出现 None。"""
from desk import report as R

SOL = {"manager": {"action": "pending", "entry_side": "short", "entry_trigger_px": 99.5, "size_pct": None, "entry": None,
                   "horizon_days": 4, "confidence": 0.6},
       "pending": {"kept": "1", "trigger_px": 99.5, "stop_loss": 104.5, "take_profit": 89.5, "kept_sz": "85.38"},
       "tm_order_tool": {"call": {"action": "keep_pending", "trigger_px": 99.5, "stop_loss": 104.5, "take_profit": 89.5,
                                  "keep_expiry": True}, "receipt": {"sz": "85.38"}}}


def test_keep_pending_shows_existing_order():
    s = R.tm_advice_text(SOL)
    assert s.startswith("保留现有挂单（") and "触发价" in s and "张数 85.38" in s and "止损" in s and "止盈" in s and "到期" in s
    assert "None" not in s and "仓位" not in s and "入场 —" not in s


def test_keep_pending_ignored_note_shown():
    d = {**SOL, "tm_order_tool": {**SOL["tm_order_tool"], "ignored_note": "保留挂单不改张数，已忽略（risk_scale=0.5）"}}
    assert "已忽略" in R.tm_advice_text(d)


def test_other_actions_no_none():
    for m in ({"action": "hold", "confidence": 0.6}, {"action": "sell"}, {"action": "reduce", "reduce_fraction": 0.3},
              {"action": "buy", "entry": 100.0, "stop_loss": 95.0, "take_profit": 110.0, "size_pct": None}):
        s = R.tm_advice_text({"manager": m})
        assert "None" not in s and s
    s = R.tm_advice_text({"manager": {"action": "reduce", "reduce_fraction": 0.3}, "tm_order_tool": {"call": {"action": "reduce"}}})
    assert "部分减仓" in s and "0.3" in s


def test_pending_line():
    d = {"manager": {"action": "pending", "entry_side": "long", "entry_trigger_px": 50.0, "entry_stop_loss": 48.0, "entry_take_profit": 56.0},
         "tm_order_tool": {"call": {"action": "pending", "side": "long", "trigger_px": 50.0, "margin_pct": None}}}
    s = R.tm_advice_text(d)
    assert "条件入场计划（多" in s and "None" not in s
