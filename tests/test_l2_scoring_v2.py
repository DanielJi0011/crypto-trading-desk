"""L2 打分 v2 验收：基础率扣除、各自周期、条件单触发、ATR 带宽、同日多币一个样本、R 倍数。离线。"""
import datetime as dt
import math

import pandas as pd
import pytest

from desk import scoring, settings

NOW = dt.datetime(2027, 6, 1, tzinfo=settings.TZ)


@pytest.fixture
def clean():
    for p in (scoring.STANCES, scoring.SCORES, scoring.WEIGHTS, scoring.WEIGHTS_SHADOW, scoring.WEIGHT_HIST):
        if p.exists():
            p.unlink()
    yield
    for p in (scoring.STANCES, scoring.SCORES, scoring.WEIGHTS, scoring.WEIGHTS_SHADOW, scoring.WEIGHT_HIST):
        if p.exists():
            p.unlink()


def _df(closes, start="2025-06-01", hl=0.01):
    days = pd.date_range(start, periods=len(closes))
    return pd.DataFrame({"Date": days, "Open": closes, "High": [c * (1 + hl) for c in closes],
                         "Low": [c * (1 - hl) for c in closes], "Close": closes})


def _st(i, role, direction, date, ref, inst="BTC-USDT-SWAP", **kw):
    s = {"id": f"S{i}:{role}:{inst}", "decision_id": f"S{i}", "date": date, "time": f"{date} 08:00:00+0800",
         "inst_id": inst, "role": role, "direction": direction, "confidence": 0.6, "target": None, "stop": None,
         "horizon_days": 5, "ref_px": ref, "regime": "上涨"}
    s.update(kw)
    settings.append_jsonl(scoring.STANCES, s)


def _price_on(df, date):
    return float(df[df.Date < pd.Timestamp(date)].Close.iloc[-1])


def _run(df, stances_fn, cfg=None):
    cfg = cfg or settings.load_config()
    scoring.DAILY = lambda inst: df
    stances_fn()
    return cfg, scoring.score_due(cfg, now=NOW)


def test_always_bull_in_uptrend_has_no_skill(clean, monkeypatch):
    closes = [100 * 1.01 ** i for i in range(500)]          # 每天 +1%：任何 5 天窗口都是"涨"
    df = _df(closes); monkeypatch.setattr(scoring, "DAILY", lambda inst: df)
    cfg = settings.load_config(); cfg["learning"]["apply_weights"] = False
    dates = [str(dt.date(2026, 7, 1) + dt.timedelta(days=i)) for i in range(40)]
    for i, d in enumerate(dates):
        _st(i, "market", "bullish", d, _price_on(df, d))
    new = scoring.score_due(cfg, now=NOW)
    assert len(new) == 40 and all(x["hit"] for x in new)
    assert all(x["base_rate"] == 1.0 and x["skill"] == 0 for x in new)   # 命中全靠行情本身
    scoring.update_weights(cfg)
    assert settings.read_json(scoring.WEIGHTS_SHADOW, {})["market"] == pytest.approx(1.0)


def test_always_neutral_in_flat_market_has_no_skill(clean, monkeypatch):
    closes = [100 + (0.1 if i % 2 else -0.1) for i in range(500)]   # 横盘
    df = _df(closes, hl=0.002); monkeypatch.setattr(scoring, "DAILY", lambda inst: df)
    cfg = settings.load_config()
    dates = [str(dt.date(2026, 7, 1) + dt.timedelta(days=i)) for i in range(30)]
    for i, d in enumerate(dates):
        _st(i, "news", "neutral", d, _price_on(df, d))
    new = scoring.score_due(cfg, now=NOW)
    assert all(x["hit"] and x["skill"] == 0 and x["band_src"] == "atr" for x in new)
    scoring.update_weights(cfg)
    assert settings.read_json(scoring.WEIGHTS_SHADOW, {})["news"] == pytest.approx(1.0)


def test_own_horizon_and_atr_band(clean, monkeypatch):
    closes = [100 * 1.01 ** i for i in range(500)]
    df = _df(closes); monkeypatch.setattr(scoring, "DAILY", lambda inst: df)
    d = "2026-07-01"
    _st(1, "market", "bullish", d, _price_on(df, d), horizon_days=12)
    _st(2, "flows", "bullish", d, _price_on(df, d), horizon_days=99)    # 夹到 30
    new = {x["role"]: x for x in scoring.score_due(settings.load_config(), now=NOW)}
    assert new["market"]["horizon"] == 12 and new["flows"]["horizon"] == 30
    atr = scoring._atr_pct(df, dt.date(2026, 7, 1))
    assert new["market"]["band_pct"] == pytest.approx(0.5 * atr * math.sqrt(12), abs=1e-3)


def test_conditional_entry_not_triggered_and_triggered(clean, monkeypatch):
    closes = [100.0] * 400 + [100, 101, 102, 103, 104, 106, 108, 110, 112, 114, 116, 118, 120] + [120.0] * 87
    df = _df(closes, hl=0.001); monkeypatch.setattr(scoring, "DAILY", lambda inst: df)
    d = str(df.Date.iloc[400].date())
    _st(1, "market", "bullish", d, 100.0, entry_condition="gte", entry_px=150.0)     # 从未到 150
    _st(2, "news", "bullish", d, 100.0, entry_condition="gte", entry_px=104.0)       # 第 5 天触发
    new = {x["role"]: x for x in scoring.score_due(settings.load_config(), now=NOW)}
    assert new["market"]["outcome"] == "not_triggered" and new["market"]["hit"] is None
    assert new["news"]["triggered"] == str(df.Date.iloc[404].date())
    assert scoring.scores("market") == [] and len(scoring.scores("market", include_untriggered=True)) == 1
    assert scoring.scorecard("market")["not_triggered"] == 1


def test_same_day_three_coins_one_sample(clean, monkeypatch):
    rows = [{"date": "2026-07-01", "skill": 0.6}, {"date": "2026-07-01", "skill": 0.0}, {"date": "2026-07-01", "skill": 0.3},
            {"date": "2026-07-02", "skill": -0.3}]
    for i, r in enumerate(rows):
        settings.append_jsonl(scoring.SCORES, {"id": f"x{i}", "role": "market", "time": f"{r['date']} 08:00:0{i}", "hit": True,
                                               "scoring_v": 2, **r})
    ds = scoring.daily_skill("market")
    assert ds == pytest.approx([0.3, -0.3])
    cfg = settings.load_config()
    scoring.update_weights(cfg)
    assert settings.read_json(scoring.WEIGHTS_SHADOW, {})["market"] == pytest.approx(1.0)   # Σ=0


def test_shrinkage_and_clip(clean):
    for i in range(200):
        settings.append_jsonl(scoring.SCORES, {"id": f"x{i}", "role": "market", "time": f"2026-01-01 08:{i // 60:02d}:{i % 60:02d}",
                                               "date": str(dt.date(2026, 1, 1) + dt.timedelta(days=i)), "hit": True,
                                               "scoring_v": 2, "skill": 0.5})
    cfg = settings.load_config()
    scoring.update_weights(cfg)
    w = settings.read_json(scoring.WEIGHTS_SHADOW, {})["market"]
    assert w == pytest.approx(min(math.exp(4 * 100 / 230), 2.0))   # 被夹到 2.0


def test_plan_r_multiple():
    path = _df([100, 101, 103, 106], start="2026-01-01", hl=0.0)
    r, why = scoring.plan_r({"direction": "bullish", "stop": 95.0, "target": 105.0}, path, 100.0)
    assert (r, why) == (1.0, "target")
    r, why = scoring.plan_r({"direction": "bearish", "stop": 102.0}, path, 100.0)
    assert (r, why) == (-1.0, "stop")
    assert scoring.plan_r({"direction": "bullish"}, path, 100.0) == (None, "no_plan")


def test_brier_and_buckets(clean):
    settings.append_jsonl(scoring.SCORES, {"id": "a", "role": "market", "time": "2026-01-01 08:00:00", "date": "2026-01-01",
                                           "hit": True, "scoring_v": 2, "skill": 0.1, "confidence": 0.9, "brier": 0.0,
                                           "regime": "震荡", "direction": "bullish"})
    c = scoring.scorecard("market")
    assert c["conf_buckets"]["≥0.7"]["n"] == 1 and c["brier"] == 0.0 and c["skill"] == 0.1
