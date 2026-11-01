"""feat/tm-keep-display-fix 第 1 点：keep_pending 带张数字段不拒绝，记录里忽略并在回执注明。"""
from desk import tm_order_tool as T
from tests.test_tm_order_tool import env, _go, OTC, PEND, EX  # noqa: F401
from tests.test_tm_no_vote import desk_env  # noqa: F401
from tests.test_tm_tool_receipt_expiry import _ex

KEEP = {**PEND, "action": "keep_pending", "expire_hours": None, "keep_expiry": True}


def test_keep_with_size_fields_ignored_not_rejected(env):
    m, calls, box = env
    box["ex"] = _ex()
    rec = _go(m, box, [OTC(**{**KEEP, "risk_scale": 0.5, "margin_pct": 12})])
    t = rec["tm_order_tool"]
    assert t["attempts"] == 1 and not t["rejected"] and rec["final_action"] == "pending"
    assert t["ignored"] == {"risk_scale": 0.5, "margin_pct": 12}
    assert "保留挂单不改张数，已忽略" in t["ignored_note"]
    assert t["call"]["risk_scale"] == 1.0 and t["call"]["margin_pct"] is None
    assert "保留挂单不改张数，已忽略" in (t["receipt"] or {}).get("note", "")


def test_keep_without_size_fields_no_note(env):
    m, calls, box = env
    box["ex"] = _ex()
    t = _go(m, box, [OTC(**{**KEEP, "margin_pct": None})])["tm_order_tool"]
    assert not t["ignored"] and not t["ignored_note"]


def test_pending_size_fields_not_ignored():
    c = OTC(**{**PEND, "risk_scale": 0.5})
    assert T.keep_ignored_fields(c) == {}
    assert T.strip_keep_size_fields(c)[0].risk_scale == 0.5


def test_prompt_and_schema_say_keep_size_fields_useless(env):
    m, calls, box = env
    _go(m, box, [OTC(**PEND)])
    p = calls["prompts"][0]
    assert "keep_pending（保留挂单）沿用原挂单张数：risk_scale、margin_pct 等张数字段不起作用，不要填" in p
    f = T.OrderToolCall.model_fields
    assert "不起作用" in f["risk_scale"].description and "不起作用" in f["margin_pct"].description
