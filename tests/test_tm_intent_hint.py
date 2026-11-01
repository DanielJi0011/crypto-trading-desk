"""第 2 点：理由里未落地的操作意图只做关键词提示，不做动作。"""
from desk import tm_order_tool as T
from desk import report as R


def test_keywords_detected():
    assert T.untriggered_intents("92.26附近可考虑部分止盈（另行处理）") == ["部分止盈", "另行处理"]
    assert T.untriggered_intents("回头再看") == ["回头再"]
    assert T.untriggered_intents("正常理由") == [] and T.untriggered_intents(None) == []


def test_report_hint_line():
    d = {"manager": {"rationale": "可考虑部分止盈（另行处理）"}}
    line = R.untriggered_intent_line(d)
    assert "未落地" in line and "部分止盈" in line and "action 字段" in line
    assert R.untriggered_intent_line({"manager": {"rationale": "无"}}) is None


def test_schema_rationale_not_executed():
    assert "都不会被执行" in T.OrderToolCall.model_fields["rationale"].description
