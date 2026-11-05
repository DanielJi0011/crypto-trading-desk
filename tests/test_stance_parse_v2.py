"""立场行解析 v2（2026-11-02）：逐字段宽容解析 + 解析健康统计。"""
from desk import stances

MARK = "【立场】"


def test_canonical_line_unchanged():
    s = stances.parse_stance("正文\n" + MARK + "方向=看空；目标价=70,000；止损价=无；置信度=0.7；周期=3天")
    assert s["direction"] == "bearish" and s["target"] == 70000 and s["stop"] is None
    assert s["confidence"] == 0.7 and s["horizon_days"] == 3 and not s.get("partial")


def test_field_order_free():
    s = stances.parse_stance(MARK + "置信度=0.6；周期=5天；方向=看多；止损价=78500；目标价=92000")
    assert s["direction"] == "bullish" and s["target"] == 92000 and s["stop"] == 78500
    assert s["confidence"] == 0.6 and s["horizon_days"] == 5


def test_missing_target_stop_fields_ok():
    s = stances.parse_stance(MARK + "方向=中性；置信度=0.5；周期=7天")
    assert s["direction"] == "neutral" and s["target"] is None and s["stop"] is None
    assert not s.get("partial")


def test_confidence_percent_and_horizon_units():
    s = stances.parse_stance(MARK + "方向=偏多；目标价=100；止损价=90；置信度=65%；周期=5 days")
    assert s["direction"] == "bullish" and s["confidence"] == 0.65 and s["horizon_days"] == 5
    s2 = stances.parse_stance(MARK + "方向=看空；置信度=45；周期=3日；目标价=无；止损价=无")
    assert s2["confidence"] == 0.45 and s2["horizon_days"] == 3


def test_partial_stance_flagged():
    s = stances.parse_stance(MARK + "方向=看多；目标价=92000")
    assert s["direction"] == "bullish" and s["confidence"] == 0.5 and s["horizon_days"] is None
    assert s.get("partial") is True


def test_complete_beats_later_partial():
    txt = (MARK + "方向=看空；目标价=70000；止损价=75000；置信度=0.7；周期=3天\n"
           "补充：" + MARK + "方向=看多")
    s = stances.parse_stance(txt)
    assert s["direction"] == "bearish" and s["confidence"] == 0.7 and not s.get("partial")


def test_last_complete_wins():
    txt = (MARK + "方向=看多；目标价=1；止损价=2；置信度=0.5；周期=3天\n"
           + MARK + "方向=看空；目标价=3；止损价=4；置信度=0.6；周期=5天")
    s = stances.parse_stance(txt)
    assert s["direction"] == "bearish" and s["horizon_days"] == 5


def test_no_stance_line():
    assert stances.parse_stance("没有任何立场") is None
    assert stances.parse_stance("") is None


def test_entry_and_pending_still_parsed():
    s = stances.parse_stance(MARK + "方向=看空；目标价=76400；止损价=81950；置信度=0.6；周期=5天；"
                             "入场价=79900；入场条件=涨到；对现有挂单=保留")
    assert s["entry_px"] == 79900 and s["entry_condition"] == "gte" and s["pending_action"] == "keep"


def test_health_counts_and_degraded():
    w = {"market": 1.0, "bull": 1.0, "trade_manager": 1.0}
    decs = [{"inst_id": "BTC-USDT-SWAP", "stances": {
        "market": {"direction": "bearish", "source": "立场行", "status": "parsed"},
        "bull": {"direction": "bullish", "source": "LLM抽取", "status": "extracted"},
        "trade_manager": {"direction": "neutral", "source": "抽取失败:X", "status": "failed"}}}]
    h = stances.health(decs, w)
    assert h["tiers"]["parsed"] == 1 and h["tiers"]["extracted"] == 1
    assert [b["role"] for b in h["degraded"]] == ["bull"]
    assert stances.degraded({"source": "立场行", "partial": True})
    assert not stances.degraded({"source": "立场行", "status": "parsed"})
