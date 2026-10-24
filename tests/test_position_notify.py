"""开/平仓通知：ws 推送、重连补查、轮询快照对比共用 posId+cTime 去重（不连网、不下单）。"""
from desk import demo_ws as W

T0 = 1792596000000


def P(pid, ct, inst="SOL-USDT-SWAP", ps="short", pos="97.75", px="108.71"):
    return {"posId": pid, "cTime": str(ct), "instId": inst, "posSide": ps, "pos": pos, "avgPx": px}


def _push(pid, ct, pos, inst="SOL-USDT-SWAP", ps="short", ut=None):
    return {"arg": {"channel": "positions", "instType": "SWAP"}, "data": [
        {"posId": pid, "cTime": str(ct), "uTime": str(ut or ct), "instId": inst, "posSide": ps, "pos": pos, "avgPx": "108.71"}]}


def test_first_snapshot_is_baseline_and_restart_no_renotify(tmp_path):
    f, sent = tmp_path / "seen.json", []
    assert W.sync_snapshot([P("1", T0), P("2", T0)], "t", sent.append, f) == []
    assert W.sync_snapshot([P("1", T0), P("2", T0)], "t", sent.append, f) == []
    W.State(f).save()                                   # ws 进程保存不应抹掉快照
    assert W.sync_snapshot([P("1", T0), P("2", T0)], "t", sent.append, f) == [] and sent == []


def test_snapshot_open_and_close_with_offline_flag(tmp_path):
    f, sent = tmp_path / "seen.json", []
    W.sync_snapshot([P("1", T0, "ETH-USDT-SWAP", "long")], "t", sent.append, f, now_ms=T0)
    W.ws_down_mark("business", start_ms=T0 + 1000, path=f)
    W.ws_down_mark("business", end_ms=T0 + 900_000, path=f)
    W.sync_snapshot([P("1", T0, "ETH-USDT-SWAP", "long"), P("3", T0 + 300_000)], "demo_ws 重连补查", sent.append, f, now_ms=T0 + 910_000)
    assert len(sent) == 1
    e = sent[0]; p = W.payload(e)
    assert e["kind"] == "position_open" and p["inst_id"] == "SOL-USDT-SWAP" and p["side"] == "short"
    assert p["size"] == "97.75" and p["fill_price"] == "108.71" and p["offline"] is True
    assert "+0800" in p["event_time"] and "断线期间" in e["message"]
    W.sync_snapshot([P("1", T0, "ETH-USDT-SWAP", "long"), P("3", T0 + 300_000)], "t", sent.append, f, now_ms=T0 + 920_000)
    assert len(sent) == 1                                # 不重复
    W.sync_snapshot([P("3", T0 + 300_000)], "t", sent.append, f, now_ms=T0 + 2_000_000)
    assert sent[-1]["kind"] == "position_closed" and sent[-1]["inst_id"] == "ETH-USDT-SWAP" and sent[-1]["offline"] is False
    assert W.notify.__name__ and "position_open" in W.NOTIFY_KINDS and "position_closed" in W.NOTIFY_KINDS


def test_ws_push_then_poll_only_one_notification(tmp_path):
    f, sent = tmp_path / "seen.json", []
    W.sync_snapshot([], "t", sent.append, f)
    W.ws_position(W.parse(_push("9", T0, "5"))[0], f, sent.append, grace=0)
    W.ws_position(W.parse(_push("9", T0, "5"))[0], f, sent.append, grace=0)   # 重连后快照推送重复
    W.sync_snapshot([P("9", T0, pos="5")], "demo_watch 轮询", sent.append, f)
    assert [e["kind"] for e in sent] == ["position_open"]
    W.ws_position(W.parse(_push("9", T0, "0", ut=T0 + 5000))[0], f, sent.append, grace=0)
    W.sync_snapshot([], "demo_watch 轮询", sent.append, f)
    assert [e["kind"] for e in sent] == ["position_open", "position_closed"]


def test_ws_before_baseline_does_nothing(tmp_path):
    f, sent = tmp_path / "seen.json", []
    assert W.ws_position(W.parse(_push("9", T0, "5"))[0], f, sent.append, grace=0) is None and sent == []


def test_trigger_fill_notification_suppresses_position_open(tmp_path, monkeypatch):
    """复现 21:04 ETH：条件单触发 + 成交已发通知 → 随后的 position_open 不再重复发。"""
    monkeypatch.setattr(W, "EVENTS", tmp_path / "ev.jsonl")
    f = tmp_path / "seen.json"; st = W.State(f); sent = []
    W.sync_snapshot([], "t", sent.append, f)
    monkeypatch.setattr(W, "POS_GRACE", 0)
    algo = {"arg": {"channel": "orders-algo"}, "data": [{"instId": "ETH-USDT-SWAP", "algoId": "A7", "ordId": "7",
            "ordType": "trigger", "state": "effective", "side": "buy", "posSide": "long", "actualSz": "46.18", "actualPx": "2482.7"}]}
    order = {"arg": {"channel": "orders"}, "data": [{"instId": "ETH-USDT-SWAP", "ordId": "7", "side": "buy", "posSide": "long",
             "state": "filled", "fillSz": "46.18", "fillPx": "2482.7", "accFillSz": "46.18", "avgPx": "2482.7",
             "fillPnl": "0", "tradeId": "x", "category": "normal", "algoId": "A7"}]}
    import desk.entry_limit as EL
    monkeypatch.setattr(EL, "record_trigger", lambda *a, **k: None)
    for m in (algo, order, _push("E1", T0, "46.18", "ETH-USDT-SWAP", "long")):
        for ev in W.parse(m):
            W.handle(ev, st, cfg={}, rediscuss=lambda c, t: "x", notifier=sent.append)
    assert [e["kind"] for e in sent] == ["trigger_fired"]
    W.sync_snapshot([P("E1", T0, "ETH-USDT-SWAP", "long", "46.18")], "demo_watch 轮询", sent.append, f)
    assert len(sent) == 1


def test_missed_fill_position_open_notified_via_handle(tmp_path, monkeypatch):
    """复现 23:38 SOL：断线期间触发成交，只收到 positions 推送 → 也要发开仓通知。"""
    monkeypatch.setattr(W, "EVENTS", tmp_path / "ev.jsonl")
    f = tmp_path / "seen.json"; st = W.State(f); sent = []
    W.sync_snapshot([P("1", T0, "ETH-USDT-SWAP", "long")], "t", sent.append, f)
    monkeypatch.setattr(W, "POS_GRACE", 0)
    W.ws_down_mark("private", start_ms=T0, path=f)
    W.ws_down_mark("private", end_ms=T0 + 1_000_000, path=f)
    for ev in W.parse(_push("4029684295455682561", T0 + 500_000, "97.75")):
        W.handle(ev, st, cfg={}, rediscuss=lambda c, t: "x", notifier=sent.append)
    assert len(sent) == 1 and sent[0]["kind"] == "position_open" and sent[0]["offline"] is True
    assert sent[0]["detected_by"] == "ws 推送"
