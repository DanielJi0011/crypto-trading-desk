"""模拟盘平仓 → 复盘 trade 结构（纯本地，不访问网络）。"""
from desk import demo_trades as D
from desk import settings

ROW = {"instId": "ETH-USDT-SWAP", "posId": "111", "posSide": "short", "direction": "short", "openAvgPx": "2459",
       "closeAvgPx": "2496", "openMaxPos": "54.5", "closeTotalPos": "54.5", "realizedPnl": "-215.15", "pnl": "-201.65",
       "fee": "-13.5", "fundingFee": "0", "type": "2", "cTime": "1792507161799", "uTime": "1792508233523", "lever": "1.0"}
TEST_ROW = {**ROW, "instId": "SOL-USDT-SWAP", "posId": "222", "posSide": "long", "direction": "long", "openAvgPx": "107.5",
            "closeAvgPx": "107.4", "openMaxPos": "0.01", "closeTotalPos": "0.01", "realizedPnl": "-0.0015",
            "cTime": "1792506564663", "uTime": "1792506570008"}
EVENTS = [
    {"time": "2026-10-20 22:39:24+0800", "event": "eth_backfill_ok", "inst_id": "ETH-USDT-SWAP", "sl": "2495.46", "tp": "2306.02"},
    {"time": "2026-10-20 22:29:24+0800", "event": "test_open", "inst_id": "SOL-USDT-SWAP"},
]
PAPERS = [{"trade_id": "P1", "inst_id": "ETH-USDT-SWAP", "side": "sell", "sl_px": "2495.4552", "signal_id": "D-ETH-2",
           "entry_reason": "反弹做空", "status": "open"}]


def test_link_backfill_to_paper_decision():
    info = D.link(ROW, EVENTS, {"positions": {}}, PAPERS)
    assert info["decision_id"] == "D-ETH-2" and not info["test"]
    t = D.to_trade(ROW, info, 0.1, [])
    assert t["trade_id"] == "DEMO-111-1792507161799"
    assert t["side"] == "sell" and t["exit_reason"] == "止损" and t["signal_id"] == "D-ETH-2"
    assert abs(float(t["sz"]) - 5.45) < 1e-9
    assert abs(float(t["pnl_pct"]) - (-215.15 / (2459 * 5.45) * 100)) < 1e-3
    for k in ("entry_time", "exit_time", "entry_px", "exit_px", "sl_px", "tp_px", "pnl_usd", "entry_reason"):
        assert k in t


def test_link_registry_and_trigger_filled():
    placed = {"time": "2026-10-20 22:48:31+0800", "event": "trigger_placed", "inst_id": "ETH-USDT-SWAP", "algoId": "9",
              "decision_id": "D-X", "stop_loss": 2600, "take_profit": 2300}
    filled = {"time": "2026-10-20 22:39:20+0800", "event": "trigger_filled", "inst_id": "ETH-USDT-SWAP", "algoId": "9"}
    info = D.link(ROW, [placed, filled], {"positions": {}}, [])
    assert info["decision_id"] == "D-X" and info["sl"] == 2600
    reg = {"positions": {"ETH-USDT-SWAP": {"decision_id": "D-R", "side": "short", "opened": "2026-10-20 22:39:30+0800", "from_algo": "9"}}}
    assert D.link(ROW, [placed], reg, [])["decision_id"] == "D-R"


def test_smoke_test_positions_excluded_and_ids_stable(monkeypatch):
    monkeypatch.setattr(D.settings, "read_jsonl", lambda p: EVENTS if p.name == "demo_events.jsonl" else [])
    monkeypatch.setattr(D, "paper_trades", lambda: PAPERS)
    from desk import demo_exec
    monkeypatch.setattr(demo_exec, "reg", lambda: {"positions": {}, "triggers": {}, "watch": {}})
    cts = {"ETH-USDT-SWAP": 0.1, "SOL-USDT-SWAP": 1}
    a = D.closed_trades([ROW, TEST_ROW, {**ROW, "type": "1", "posId": "333"}], cts, use_cache=False)
    assert [t["trade_id"] for t in a] == ["DEMO-111-1792507161799"]
    b = D.closed_trades([ROW, {**ROW, "cTime": "1792600000000", "uTime": "1792600100000"}], cts, use_cache=False)
    assert len({t["trade_id"] for t in b}) == 2     # posId 复用时按开仓时间区分


def test_review_candidates_merges_paper_and_demo(monkeypatch):
    from desk import ledger
    paper = [{"trade_id": "P9", "status": "closed"}, {"trade_id": "P8", "status": "open"}, {"trade_id": "P7", "status": "closed"}]
    monkeypatch.setattr(ledger.store, "read_trades", lambda: paper)
    monkeypatch.setattr(D, "closed_trades", lambda: [{"trade_id": "DEMO-1-2", "status": "closed"}, {"trade_id": "P9", "status": "closed"}])
    got = [t["trade_id"] for t in D.review_candidates({}, {"P7"}, demo=True)]
    assert got == ["P9", "DEMO-1-2"]
    assert [t["trade_id"] for t in D.review_candidates({}, set(), demo=False)] == ["P9", "P7"]


def test_review_trade_accepts_demo_trade(tmp_path, monkeypatch):
    from desk import review
    from desk import llm as dllm
    monkeypatch.setattr(review, "REVIEWS", tmp_path)
    t = D.to_trade(ROW, D.link(ROW, EVENTS, {"positions": {}}, PAPERS), 0.1, [])
    out = review.review_trade(settings.load_config(), t, dllm.UsageTracker(settings.load_config()))
    assert out["trade_id"] == t["trade_id"] and (tmp_path / f"{t['trade_id']}.json").exists()


def test_role_call_table_short_side():
    from desk import review
    dec = {"stances": {r: {"direction": "neutral"} for r in review.ROLE_KEYS}}
    dec["stances"]["bear"] = {"direction": "bearish"}; dec["stances"]["bull"] = {"direction": "bullish"}
    rows = {r["role"]: r["right"] for r in review.role_call_table({"pnl_usd": "-5", "side": "sell"}, dec)}
    assert rows["bear"] is False and rows["bull"] is True
    rows = {r["role"]: r["right"] for r in review.role_call_table({"pnl_usd": "5", "side": "buy"}, dec)}
    assert rows["bull"] is True and rows["bear"] is False


def test_report_calls_text_cost_and_title():
    from desk import report
    from desk import llm as dllm
    assert report.calls_text({"calls": 55, "errors": 3}) == "共 58 次，成功 55 次，失败 3 次"
    assert report._cost({"calls": 2, "cost_usd": 0.0, "unpriced_calls": 2}) == "单价未知"
    assert "另 1 次单价未知" in report._cost({"calls": 3, "cost_usd": 0.01, "unpriced_calls": 1})
    cfg = settings.load_config()
    assert not dllm.price_known(cfg, "grok-4.7-build") and dllm.price_known(cfg, "deepseek-flash")
    assert "未下单" in report._exec_title({"final_action": "hold(护栏拒单)", "size_pct": 11.0})
    assert report._exec_title({"final_action": "pending", "size_pct": 11.2, "execution": {"ok": True}}) == " · 仓位 11.2%"
    assert report._exec_title({"final_action": "hold", "size_pct": 0}) == ""
