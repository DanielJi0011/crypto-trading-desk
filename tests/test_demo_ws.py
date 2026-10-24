"""demo_ws 事件解析与去重（样本按 OKX docs-v5 推送格式构造，不连网、不下单）。"""
import json

from desk import demo_ws as W, settings


def _st(tmp_path):
    return W.State(tmp_path / "seen.json")


ORD_FILLED = {"arg": {"channel": "orders", "instType": "SWAP", "uid": "1"}, "data": [{
    "instType": "SWAP", "instId": "BTC-USDT-SWAP", "ordId": "111", "side": "buy", "posSide": "net",
    "state": "filled", "fillSz": "1", "fillPx": "65000", "accFillSz": "1", "avgPx": "65000", "fillPnl": "0",
    "pnl": "0", "tradeId": "t1", "category": "normal", "algoId": ""}]}
ORD_PART = {"arg": {"channel": "orders", "instType": "SWAP"}, "data": [{
    "instId": "ETH-USDT-SWAP", "ordId": "222", "side": "sell", "state": "partially_filled", "fillSz": "2",
    "fillPx": "3000", "accFillSz": "2", "tradeId": "t2", "category": "normal"}]}
ORD_LIVE = {"arg": {"channel": "orders"}, "data": [{"instId": "BTC-USDT-SWAP", "ordId": "333", "state": "live", "category": "normal"}]}
ORD_LIQ = {"arg": {"channel": "orders"}, "data": [{"instId": "SOL-USDT-SWAP", "ordId": "444", "state": "filled", "fillSz": "5",
                                                   "fillPx": "120", "pnl": "-50", "tradeId": "t4", "category": "full_liquidation"}]}


def _algo(aid, act, ot="conditional", state="effective"):
    return {"arg": {"channel": "orders-algo", "instType": "SWAP"}, "data": [{
        "instType": "SWAP", "instId": "BTC-USDT-SWAP", "algoId": aid, "ordId": "o" + aid, "ordType": ot,
        "state": state, "side": "sell", "posSide": "net", "sz": "1", "actualSz": "1", "actualPx": "64000",
        "actualSide": act, "triggerPx": "64000"}]}


def test_parse_orders():
    e = W.parse(ORD_FILLED)
    assert e[0]["kind"] == "fill" and e[0]["ordId"] == "111" and e[0]["px"] == "65000"
    assert W.parse(ORD_PART)[0]["kind"] == "partial_fill"
    assert W.parse(ORD_LIVE) == []
    assert W.parse(ORD_LIQ)[0]["kind"] == "liquidation"


def test_parse_algo():
    assert W.parse(_algo("1", "sl"))[0]["kind"] == "stop_loss"
    assert W.parse(_algo("2", "tp", "oco"))[0]["kind"] == "take_profit"
    assert W.parse(_algo("3", "", "trigger"))[0]["kind"] == "trigger_fired"
    assert W.parse(_algo("4", "", "trigger", state="live")) == []
    assert W.parse(_algo("5", "", state="canceled")) == []


def test_parse_bp_liquidation():
    m = {"arg": {"channel": "balance_and_position"}, "data": [{"pTime": "1", "eventType": "liquidation",
          "balData": [], "posData": [{"posId": "9", "instId": "BTC-USDT-SWAP", "pos": "0", "posSide": "net"}]}]}
    assert W.parse(m)[0]["kind"] == "liquidation"
    m["data"][0]["eventType"] = "snapshot"
    assert W.parse(m) == []


def test_dedup_and_positions(tmp_path):
    st = _st(tmp_path)
    ev = W.parse(ORD_FILLED)[0]
    assert st.accept(dict(ev)) and st.accept(dict(ev)) is None
    st.save(); assert W.State(tmp_path / "seen.json").accept(dict(ev)) is None   # 持久化去重
    pos = lambda p: {"arg": {"channel": "positions"}, "data": [{"posId": "p1", "instId": "BTC-USDT-SWAP", "pos": p, "posSide": "net"}]}
    assert st.accept(W.parse(pos("0"))[0]) is None            # 初始空仓不报
    assert st.accept(W.parse(pos("1"))[0])["kind"] == "position_open"
    assert st.accept(W.parse(pos("2"))[0]) is None            # 加仓不重复报新增
    assert st.accept(W.parse(pos("0"))[0])["kind"] == "position_closed"


def test_stop_triggers_rediscuss_once(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "EVENTS", tmp_path / "ev.jsonl")
    calls, sent = [], []
    rd = lambda cfg, t: calls.append(t) or "已启动"
    st = _st(tmp_path)
    for _ in range(3):   # 同一 algoId 重复推送
        W.handle(W.parse(_algo("777", "sl"))[0], st, cfg={}, rediscuss=rd, notifier=sent.append)
    assert calls == [{"inst_id": "BTC-USDT-SWAP", "trade_id": "DEMO-777", "reason": "sl"}]   # 与 demo_watch 同 trade_id
    assert sent == []          # 触发时只启动再讨论，通知等整单成交后合并
    lines = (tmp_path / "ev.jsonl").read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["kind"] == "stop_loss"


def test_shared_rediscuss_dedup_with_demo_watch(monkeypatch):
    """demo_watch 先处理过 DEMO-<algoId>，WS 再来不会二次启动（共用 rediscuss.json）。"""
    from desk import monitor
    spawned = []
    monkeypatch.setattr(monitor, "SPAWN", lambda a, lf: spawned.append(a))
    cfg = settings.load_config()
    t = {"inst_id": "DOGE-USDT-SWAP", "trade_id": "DEMO-888"}
    r1 = monitor.schedule_rediscuss(cfg, t)        # demo_watch 路径
    r2 = monitor.schedule_rediscuss(cfg, dict(t))  # WS 路径
    assert r1 == "已启动" and r2 == "该事件已讨论过" and len(spawned) == 1
    t2 = {"inst_id": "DOGE-USDT-SWAP", "trade_id": "DEMO-889"}
    assert monitor.schedule_rediscuss(cfg, t2) == "冷却中"


def test_notify_payload_and_no_env(monkeypatch, tmp_path):
    monkeypatch.delenv("TA_NOTIFY_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(W, "SECRETS", tmp_path / "none.env")
    ev = W.parse(ORD_FILLED)[0]
    assert W.notify(ev) is False                       # 未设置只记日志，不报错
    assert W.notify({"kind": "position_open"}) is False
    p = W.payload({**ev, "rediscuss": "已启动"})
    for k in ("event_type", "coin", "side", "size", "fill_price", "pnl", "order_id", "algo_id", "rediscuss_started", "time"):
        assert k in p
    assert p["coin"] == "BTC" and p["time"].endswith("+0800") or "+08" in p["time"]


def test_notify_headers(monkeypatch):
    monkeypatch.delenv("TA_NOTIFY_DISABLED", raising=False)   # 本用例验证发送逻辑本身；requests.post 已被本用例打桩
    import requests
    got = {}
    class R: status_code = 200; ok = True
    monkeypatch.setenv("TA_NOTIFY_WEBHOOK_URL", "https://example.invalid/hook")
    monkeypatch.setenv("TA_NOTIFY_WEBHOOK_KEY", "k123")
    monkeypatch.setattr(requests, "post", lambda url, data, headers, timeout: got.update(headers, timeout=timeout) or R())
    assert W.notify({"kind": "stop_loss", "inst_id": "BTC-USDT-SWAP"})
    assert got["timeout"] == 30
    assert got["Authorization"] == "Bearer k123" and got["X-Webhook-Key"] == "k123"


def test_urls_demo_only_and_sign():
    assert all("wspap.okx.com" in u for u in W.URLS.values())


def _ord(oid, st, fsz, fpx, acc, avg, pnl, tid, side="buy", pos_side="short", algo=""):
    return {"arg": {"channel": "orders", "instType": "SWAP"}, "data": [{
        "instId": "ETH-USDT-SWAP", "ordId": oid, "side": side, "posSide": pos_side, "state": st, "fillSz": fsz,
        "fillPx": fpx, "accFillSz": acc, "avgPx": avg, "fillPnl": pnl, "tradeId": tid, "category": "normal", "algoId": algo}]}


def _run(msgs, tmp_path, monkeypatch, st=None):
    monkeypatch.setattr(W, "EVENTS", tmp_path / "ev.jsonl")
    st = st or _st(tmp_path); sent, calls = [], []
    rd = lambda cfg, t: calls.append(t) or "已启动"
    for m in msgs:
        for ev in W.parse(m):
            W.handle(ev, st, cfg={}, rediscuss=rd, notifier=sent.append)
    return sent, calls


def test_partial_fills_merged_into_one(tmp_path, monkeypatch):
    msgs = [_ord("9", "partially_filled", "1", "100", "1", "100", "0", "a", side="sell"),
            _ord("9", "partially_filled", "2", "101", "3", "100.67", "0", "b", side="sell"),
            _ord("9", "filled", "1", "102", "4", "101", "0", "c", side="sell")]
    sent, _ = _run(msgs, tmp_path, monkeypatch)
    assert len(sent) == 1
    e = sent[0]; p = W.payload(e)
    assert e["kind"] == "fill" and p["acc_fill_size"] == "4" and p["avg_price"] == "101" and p["size"] == "4"
    assert p["action"] == "开仓" and p["fill_count"] == 3
    assert len((tmp_path / "ev.jsonl").read_text().splitlines()) == 3   # 每笔仍记录


def test_partial_then_canceled_notifies_once(tmp_path, monkeypatch):
    msgs = [_ord("8", "partially_filled", "1", "100", "1", "100", "-2", "a"),
            _ord("8", "partially_filled", "1", "100", "2", "100", "-3", "b"),
            _ord("8", "canceled", "", "", "2", "100", "", "")]
    sent, _ = _run(msgs, tmp_path, monkeypatch)
    assert len(sent) == 1 and sent[0]["pnl_total"] == -5 and sent[0]["action"] == "平仓"
    assert sent[0]["order_state"] == "canceled" and sent[0]["accFillSz"] == "2"


def test_canceled_without_fill_ignored(tmp_path, monkeypatch):
    sent, _ = _run([_ord("7", "canceled", "", "", "0", "", "", "")], tmp_path, monkeypatch)
    assert sent == []


def test_stop_loss_single_merged_notification(tmp_path, monkeypatch):
    """复现 22:57 ETH：止损触发 + 4 笔部分成交 + 终态 filled → 只发 1 条止损通知，再讨论在触发时即启动。"""
    algo = {"arg": {"channel": "orders-algo"}, "data": [{"instId": "ETH-USDT-SWAP", "algoId": "A1", "ordId": "9",
            "ordType": "oco", "state": "effective", "side": "buy", "posSide": "short", "actualSz": "54.5",
            "actualPx": "", "actualSide": "sl"}]}
    monkeypatch.setattr(W, "EVENTS", tmp_path / "ev.jsonl")
    st = _st(tmp_path); sent, calls = [], []
    rd = lambda cfg, t: calls.append(t) or "已启动"
    W.handle(W.parse(algo)[0], st, cfg={}, rediscuss=rd, notifier=sent.append)
    assert len(calls) == 1 and sent == []          # 第一时间启动再讨论
    fills = [("2.93", "-10.84", "2.93"), ("0.01", "-0.04", "2.94"), ("0.03", "-0.11", "2.97"), ("0.06", "-0.22", "3.03")]
    for i, (sz, pnl, acc) in enumerate(fills):
        W.handle(W.parse(_ord("9", "partially_filled", sz, "2496", acc, "2496", pnl, f"t{i}", algo="A1"))[0], st,
                 cfg={}, rediscuss=rd, notifier=sent.append)
    assert sent == []
    st2 = W.State(tmp_path / "seen.json")          # 中途重启也不丢累计与待合并止损
    W.handle(W.parse(_ord("9", "filled", "51.47", "2496", "54.5", "2496", "-190.44", "t9", algo="A1"))[0], st2,
             cfg={}, rediscuss=rd, notifier=sent.append)
    assert len(sent) == 1 and len(calls) == 1
    e = sent[0]
    assert e["kind"] == "stop_loss" and e["rediscuss"] == "已启动" and e["action"] == "平仓"
    assert e["sz"] == "54.5" and e["px"] == "2496" and abs(e["pnl_total"] - (-201.65)) < 1e-6
    assert "止损已完成" in e["message"]
    W.handle(W.parse(algo)[0], st2, cfg={}, rediscuss=rd, notifier=sent.append)   # 重复推送
    assert len(sent) == 1


def test_fill_before_algo_push_no_duplicate(tmp_path, monkeypatch):
    st = _st(tmp_path)
    sent, _ = _run([_ord("5", "filled", "1", "10", "1", "10", "-1", "x", algo="A5")], tmp_path, monkeypatch, st)
    sent2, calls = _run([_algo("A5", "sl")], tmp_path, monkeypatch, st)
    assert len(sent) == 1 and sent2 == [] and len(calls) == 1


def test_notify_retries(monkeypatch):
    monkeypatch.delenv("TA_NOTIFY_DISABLED", raising=False)   # 本用例验证发送逻辑本身；requests.post 已被本用例打桩
    import requests
    n = []
    class R: status_code = 200; ok = True
    def post(url, data, headers, timeout):
        n.append(timeout)
        if len(n) < 3:
            raise requests.ReadTimeout()
        return R()
    monkeypatch.setenv("TA_NOTIFY_WEBHOOK_URL", "https://example.invalid/hook")
    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(W.time, "sleep", lambda s: None)
    assert W.notify({"kind": "fill"}) and n == [30, 30, 30]
    n.clear(); monkeypatch.setattr(requests, "post", lambda *a, **k: n.append(1) or (_ for _ in ()).throw(requests.ReadTimeout()))
    assert W.notify({"kind": "fill"}) is False and len(n) == 3
