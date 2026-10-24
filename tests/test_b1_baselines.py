import pandas as pd
import pytest

from desk import baselines, settings


@pytest.fixture(autouse=True)
def iso(tmp_path, monkeypatch):
    monkeypatch.setattr(baselines, "BOOK", tmp_path / "book.json")
    monkeypatch.setattr(baselines, "LOG", tmp_path / "b.jsonl")


def _df(closes, start="2026-08-01"):
    d = pd.date_range(start, periods=len(closes), freq="D")
    return pd.DataFrame({"Date": d, "Open": closes, "High": [c * 1.01 for c in closes],
                         "Low": [c * 0.99 for c in closes], "Close": closes})


def _cfg():
    cfg = settings.load_config()
    cfg["coins"] = [{"inst_id": "BTC-USDT-SWAP", "ticker": "BTC"}]
    return cfg


def _rec(dirs):
    st = {r: {"direction": d, "confidence": 0.8, "status": "structured"} for r, d in dirs.items()}
    return {"inst_id": "BTC-USDT-SWAP", "stances": st, "weights": {}}


def test_ablation_subsets():
    rec = _rec({"trade_manager": "bullish", "market": "bearish", "sentiment": "bearish", "news": "bearish",
                "flows": "bearish", "research_manager": "neutral"})
    assert baselines.ablation_side(rec, ["trade_manager"], 0.1) == "long"
    assert baselines.ablation_side(rec, list(baselines.ANALYSTS), 0.1) == "short"
    assert baselines.ablation_side(rec, ["research_manager"], 0.1) is None
    rec["stances"]["trade_manager"]["status"] = "failed"
    assert baselines.ablation_side(rec, ["trade_manager"], 0.1) is None


def test_random_fixed_seed():
    a = [baselines.random_side(1, "2026-10-0%d" % i, "BTC") for i in range(1, 10)]
    assert a == [baselines.random_side(1, "2026-10-0%d" % i, "BTC") for i in range(1, 10)]
    assert set(a) <= {"long", "short"}


def test_update_open_stop_and_costs():
    cfg = _cfg()
    up = _df([100.0 + i for i in range(60)])
    rec = _rec({"trade_manager": "bullish"})
    out = baselines.update(cfg, "2026-09-29", [rec], candles=lambda i: up, funding=lambda i: 0.0001)
    groups = {r["group"] for r in out if r["kind"] == "open"}
    assert {"buy_hold", "ma_trend", "tm_only"} <= groups
    assert baselines.update(cfg, "2026-09-29", [rec], candles=lambda i: up, funding=lambda i: 0.0001) == []  # 同日不重复
    book = settings.read_json(baselines.BOOK, {})
    p = book["ma_trend"]["open"]["BTC-USDT-SWAP"]
    assert p["side"] == "long" and p["stop"] < p["entry"] < p["tp"]
    assert p["risk_usd"] <= 10000 * 0.02 + 1e-6                     # R4 上限内
    crash = pd.concat([up, _df([50.0], "2026-09-30")], ignore_index=True)
    out = baselines.update(cfg, "2026-09-30", [], candles=lambda i: crash, funding=lambda i: 0.0001)
    c = [r for r in out if r["kind"] == "close" and r["group"] == "ma_trend"][0]
    assert c["why"] == "止损" and c["fee"] > 0 and c["funding"] > 0 and c["pnl"] < 0
    s = baselines.stats(cfg)
    assert s["ma_trend"]["n"] == 1 and "buy_hold" in s
    assert any("对照组" in x for x in baselines.report_lines(cfg))
