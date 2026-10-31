"""feat/report-tool-sizing：下单工具预检回执（张数/保证金/止损风险/占权益）、定仓心算口径与基准示例、keep_expiry。全部离线。"""
import datetime as dt

import pytest

from desk import settings, tm_order_tool as T
from tests.test_tm_order_tool import env, _go, OTC, INV, PEND, EX, POS  # noqa: F401
from tests.test_tm_no_vote import desk_env  # noqa: F401


def _ex(hours=30.0):
    return {**EX, "expires": (settings.now() + dt.timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S%z"),
            "created": (settings.now() - dt.timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S%z")}


def test_receipt_returned_on_pass(env):
    m, calls, box = env
    rec = _go(m, box, [OTC(**PEND)])
    r = rec["tm_order_tool"]["receipt"]
    assert r and r["sz"] and r["notional"] > 0 and r["margin"] > 0
    qty = float(r["sz"])   # ctVal=1
    assert r["stop_loss_usd"] == pytest.approx(qty * 3.0, abs=0.01) and r["risk_pct_equity"] > 0
    z = rec["aggregate"]["sizing"]
    assert z["stop_loss_usd"] == pytest.approx(qty * 3.0, abs=0.01) and "risk_pct_equity" in z and "margin" in z


def test_risk_sizing_info_has_risk_usd(env):
    m, calls, box = env
    rec = _go(m, box, [OTC(**{**PEND, "margin_pct": None, "risk_scale": 0.5})])
    z = rec["aggregate"]["sizing"]
    assert z["how"] == "risk_sizing" and z.get("risk_usd") and z.get("stop_loss_usd") is not None


def test_reject_message_carries_receipt(env):
    m, calls, box = env
    box["guard"] = lambda qty, add: ["[POSITION] 超限"] if qty > 300 else []
    _go(m, box, [OTC(**{**PEND, "margin_pct": 40}), OTC(**{**PEND, "margin_pct": 20})])
    p = calls["prompts"][1]
    assert "[RANGE]" in p and "止损亏损约" in p and "占权益" in p and "保证金" in p


def test_prompt_formula_baseline_and_expiry_docs(env):
    m, calls, box = env
    _go(m, box, [OTC(**PEND)])
    p = calls["prompts"][0]
    assert "risk_scale 乘以单笔风险 1.5% 的基准得到张数，请按此心算" in p
    assert "本币当前基准" in p and "止损距离 2%、risk_scale 1" in p and "止损距离 4%、risk_scale 0.5" in p
    assert "从挂单时刻起算" in p and "keep_expiry=true" in p


def test_baseline_lines_without_cfg_only_formula():
    out = T.baseline_lines(None, {})
    assert len(out) == 1 and "请按此心算" in out[0]


def test_keep_expiry_keep_pending_no_expire_hours(env):
    m, calls, box = env
    box["ex"] = _ex()
    rec = _go(m, box, [OTC(**{**PEND, "action": "keep_pending", "expire_hours": None, "keep_expiry": True})])
    assert rec["final_action"] == "pending" and calls["expiry"] == [("OLD", None)]
    assert any("keep_expiry" in n and "不按 expire_hours 重算" in n for n in rec["notes"])
    assert "原挂单时刻" in calls["prompts"][0]


def test_keep_expiry_ignores_given_expire_hours(env):
    m, calls, box = env
    box["ex"] = _ex()
    _go(m, box, [OTC(**{**PEND, "action": "keep_pending", "expire_hours": 5, "keep_expiry": True})])
    assert calls["expiry"] == [("OLD", None)]


def test_keep_pending_without_expiry_still_required(env):
    m, calls, box = env
    box["ex"] = _ex()
    _go(m, box, [OTC(**{**PEND, "action": "keep_pending", "expire_hours": None}), OTC(**{**PEND, "action": "keep_pending"})])
    assert "[MISSING]" in calls["prompts"][1] and "expire_hours" in calls["prompts"][1]


def test_keep_expiry_replace_uses_remaining_hours(env):
    m, calls, box = env
    box["ex"] = _ex(30.0)
    rec = _go(m, box, [OTC(**{**PEND, "action": "replace_pending", "trigger_px": 103.0, "stop_loss": 106.0, "take_profit": 97.0,
                              "expire_hours": None, "keep_expiry": True})])
    o = calls["place_trigger"][0][0][3]
    assert o["expire_hours"] == pytest.approx(30.0, abs=0.05)
    assert any("沿用原到期时刻" in n for n in rec["notes"])


def test_keep_expiry_rejected_for_new_pending(env):
    m, calls, box = env
    _go(m, box, [OTC(**{**PEND, "keep_expiry": True}), OTC(**PEND)])
    assert "[EXPIRY]" in calls["prompts"][1]


def test_keep_expiry_rejected_when_original_expired_or_missing(env):
    m, calls, box = env
    box["ex"] = {**EX, "expires": (settings.now() - dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S%z")}
    _go(m, box, [OTC(**{**PEND, "action": "keep_pending", "expire_hours": None, "keep_expiry": True}),
                 OTC(**{**PEND, "action": "keep_pending"})])
    assert "[EXPIRY]" in calls["prompts"][1]
    box["ex"], calls["prompts"][:] = {**EX, "expires": None}, []
    _go(m, box, [OTC(**{**PEND, "action": "replace_pending", "expire_hours": None, "keep_expiry": True}),
                 OTC(**{**PEND, "action": "replace_pending"})])
    assert "[EXPIRY]" in calls["prompts"][1] and "未登记" in calls["prompts"][1]


def test_remaining_hours_parsing():
    now = settings.now()
    assert T.remaining_hours((now + dt.timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S%z")) == pytest.approx(3, abs=0.01)
    assert T.remaining_hours((now + dt.timedelta(hours=3)).isoformat()) == pytest.approx(3, abs=0.01)
    assert T.remaining_hours(None) is None and T.remaining_hours("bad") is None
    assert T.remaining_hours((now - dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S%z")) is None
