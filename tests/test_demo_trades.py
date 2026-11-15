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


# ---- 回归：watch 检测滞后 >180s 时条件单成交关联丢失（2026-11-13 SOL / 2026-11-15 ETH 实盘事故）----
# 两笔的共同形态：trigger_placed 在下单时（离成交很久），trigger_fired 由 watch 轮询在成交后
# 245s/283s 才落盘，超过 link 的 ±180s 窗口；registry 登记又已随平仓清除 → 旧链路关联为空，
# 复盘记录全空白。entry_limit 登记的 triggered_ms 是交易所时刻（与 cTime 差 <1s）且平仓后保留。
ETH_ROW_1115 = {"instId": "ETH-USDT-SWAP", "posId": "4100553274608529411", "posSide": "short", "direction": "short",
                "openAvgPx": "1751.75", "closeAvgPx": "1850", "openMaxPos": "38.87", "closeTotalPos": "38.87",
                "realizedPnl": "-378.244006", "pnl": "-381.89775", "fee": "-4.957285", "fundingFee": "8.6110295106800343",
                "type": "2", "cTime": "1794708410657", "uTime": "1794733873000", "lever": "1.0"}
ETH_EVENTS_1115 = [
    {"time": "2026-11-15 09:24:05+0800", "event": "trigger_placed", "inst_id": "ETH-USDT-SWAP",
     "algoId": "4100467210383167488", "decision_id": "D-20261115-ETH-2", "stop_loss": 1850.0, "take_profit": 1590.0},
    {"time": "2026-11-15 10:11:33+0800", "event": "trigger_fired", "inst_id": "ETH-USDT-SWAP",
     "algoId": "4100467210383167488", "ordId": "4100553271219531777"},   # 成交后 283s，窗口外
]
ETH_ENTRY_1115 = {"inst": "ETH-USDT-SWAP", "side": "short", "pos_side": "short", "sz": "38.87", "result": "filled",
                  "decision_id": "D-20261115-ETH-2", "stop_loss": 1850.0, "take_profit": 1590.0,
                  "triggered_ms": 1794708410618, "algoId": "4100467210383167488", "ordId": "4100553271219531777"}

SOL_ROW_1113 = {"instId": "SOL-USDT-SWAP", "posId": "4090389049651179521", "posSide": "short", "direction": "short",
                "openAvgPx": "81.68", "closeAvgPx": "76.33", "openMaxPos": "136.9", "closeTotalPos": "136.9",
                "realizedPnl": "727.119024", "pnl": "732.415", "fee": "-7.461187", "fundingFee": "2.1652104",
                "type": "2", "cTime": "1794576774761", "uTime": "1794622674000", "lever": "1.0"}
SOL_EVENTS_1113 = [
    {"time": "2026-11-13 11:06:25+0800", "event": "trigger_placed", "inst_id": "SOL-USDT-SWAP",
     "algoId": "4094875006566547456", "decision_id": "D-20261113-SOL-2", "stop_loss": 85.0, "take_profit": 76.3},
    {"time": "2026-11-13 21:36:59+0800", "event": "trigger_fired", "inst_id": "SOL-USDT-SWAP",
     "algoId": "4094875006566547456", "ordId": "4096136299740344321"},   # 成交后 245s，窗口外
]
SOL_ENTRY_1113 = {"inst": "SOL-USDT-SWAP", "side": "short", "pos_side": "short", "sz": "136.9", "result": "filled",
                  "decision_id": "D-20261113-SOL-2", "stop_loss": 85.0, "take_profit": 76.3,
                  "triggered_ms": 1794576774605, "algoId": "4094875006566547456", "ordId": "4096136299740344321"}


def test_link_watch_lag_blank_without_entry_limit():
    # 复现事故：没有 entry_limit 来源时，两笔都关联为空（旧行为）
    assert D.link(ETH_ROW_1115, ETH_EVENTS_1115, {"positions": {}}, [])["decision_id"] is None
    assert D.link(SOL_ROW_1113, SOL_EVENTS_1113, {"positions": {}}, [])["decision_id"] is None


def test_link_entry_limit_recovers_lagged_fills():
    info = D.link(ETH_ROW_1115, ETH_EVENTS_1115, {"positions": {}}, [], [ETH_ENTRY_1115])
    assert info["decision_id"] == "D-20261115-ETH-2" and info["sl"] == 1850.0 and info["tp"] == 1590.0
    assert info["source"].startswith("entry_limit(")
    t = D.to_trade(ETH_ROW_1115, info, 0.1, [])
    assert t["signal_id"] == "D-20261115-ETH-2" and t["sl_px"] == "1850" and t["tp_px"] == "1590"
    info = D.link(SOL_ROW_1113, SOL_EVENTS_1113, {"positions": {}}, [], [SOL_ENTRY_1113])
    assert info["decision_id"] == "D-20261113-SOL-2" and info["sl"] == 85.0 and info["tp"] == 76.3


def test_link_entry_limit_guards():
    base = [ETH_ENTRY_1115]
    assert D.link(ETH_ROW_1115, [], {"positions": {}}, [],
                  [{**ETH_ENTRY_1115, "result": "algo_canceled"}])["decision_id"] is None      # 未成交不关联
    assert D.link(ETH_ROW_1115, [], {"positions": {}}, [],
                  [{**ETH_ENTRY_1115, "side": "long"}])["decision_id"] is None                  # 方向不符
    assert D.link(ETH_ROW_1115, [], {"positions": {}}, [],
                  [{**ETH_ENTRY_1115, "triggered_ms": 1794708410618 + 3_600_000}])["decision_id"] is None  # 加仓腿等晚成交
    assert D.link(ETH_ROW_1115, [], {"positions": {}}, [],
                  [{**ETH_ENTRY_1115, "inst": "SOL-USDT-SWAP"}])["decision_id"] is None        # 品种不符
    near = {**ETH_ENTRY_1115, "decision_id": "D-NEAR", "triggered_ms": 1794708410657 + 60_000, "algoId": "X1"}
    far = {**ETH_ENTRY_1115, "decision_id": "D-FAR", "triggered_ms": 1794708410657 + 120_000, "algoId": "X2"}
    assert D.link(ETH_ROW_1115, [], {"positions": {}}, [], [far, near])["decision_id"] == "D-NEAR"  # 取最近
    reg = {"positions": {"ETH-USDT-SWAP": {"decision_id": "D-R", "side": "short",
                                           "opened": "2026-11-15 10:06:55+0800", "from_algo": "4100467210383167488"}}}
    assert D.link(ETH_ROW_1115, ETH_EVENTS_1115, reg, [], base)["decision_id"] == "D-R"          # registry 优先不变


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
