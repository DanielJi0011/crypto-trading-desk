"""L3：立场状态与有效票门槛。"""
from desk import manager, settings
from desk.stances import stance_status

MK = {"last": 100.0, "atr14": 3.0}


def _w():
    return {r: 1.0 for r in manager.VOTERS}


def _st(other):
    st = {r: dict(other) for r in manager.VOTERS}
    st["market"] = {"direction": "bullish", "confidence": 0.3, "source": "立场行", "entry_px": 100, "entry_condition": "now"}
    return st


def test_status_mapping():
    assert stance_status({"source": "抽取失败:Timeout", "direction": "neutral", "confidence": 0}) == "failed"
    assert stance_status({"source": "无发言", "direction": "neutral", "confidence": 0}) == "absent"
    assert stance_status({"source": "立场行", "direction": "neutral", "confidence": 0.5}) == "neutral_active"
    assert stance_status({"source": "LLM抽取", "direction": "bullish", "confidence": 0.5}) == "extracted"
    assert stance_status({"source": "结构化输出", "direction": "bearish", "confidence": 0.5}) == "structured"
    assert stance_status({"status": "data_missing", "source": "立场行", "direction": "bullish"}) == "data_missing"


def test_one_bull_eleven_active_neutral_no_trade():
    cfg = settings.load_config()
    st = _st({"direction": "neutral", "confidence": 0.5, "source": "立场行"})
    a = manager.aggregate(cfg, st, _w(), MK)
    assert abs(a["S"] - 0.025) < 1e-9 and not a["buy"] and a["valid_voters"] == 12


def test_one_bull_eleven_failed_no_trade():
    cfg = settings.load_config()
    st = _st({"direction": "neutral", "confidence": 0.0, "source": "抽取失败:X"})
    assert manager.consensus(st, _w()) == 0.3     # 失败票不进分母
    a = manager.aggregate(cfg, st, _w(), MK)
    assert not a["buy"] and a["valid_voters"] == 1 and any("有效票不足" in n for n in a["notes"])


def test_enough_valid_votes_can_trade():
    cfg = settings.load_config()
    st = _st({"direction": "bullish", "confidence": 0.6, "source": "立场行", "entry_px": 100, "entry_condition": "now"})
    for r in manager.VOTERS[6:]:
        st[r] = {"direction": "neutral", "confidence": 0, "source": "无发言"}
    cfg["manager"]["tm_required_for_open"] = False   # 本测试验证其他护栏，沿用无交易经理点位的旧回退
    a = manager.aggregate(cfg, st, _w(), MK)
    assert a["valid_voters"] == 6 and a["buy"]


def test_invalid_stances_not_scored(monkeypatch):
    from desk import scoring
    settings.append_jsonl(scoring.STANCES, {"id": "x1", "role": "market", "inst_id": "BTC-USDT-SWAP", "date": "2026-01-01",
                                            "direction": "neutral", "confidence": 0, "source": "抽取失败:X", "horizon_days": 1})
    monkeypatch.setattr(scoring, "okx_data", None, raising=False)
    assert scoring.score_due(settings.load_config()) == [] or all(r["id"] != "x1" for r in scoring.scores(include_untriggered=True))
