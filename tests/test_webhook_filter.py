"""webhook 降噪：重连补推不重发、真新开仓仍发、告警冷却窗口、stale_code 分支条件（离线）。"""
import time

from desk import demo_ws as W


def _pos(pid, ctime, pos="1"):
    return {"kind": "position_update", "pos_id": pid, "pos": float(pos), "inst_id": "SOL-USDT-SWAP",
            "posSide": "long", "side": "long", "px": "100", "cTime": str(ctime), "uTime": str(ctime)}


def test_reconnect_replay_fill_not_resent(tmp_path):
    st = W.State(path=tmp_path / "seen.json")
    sent = []
    fill = {"kind": "fill", "key": "ord:1:filled:t1", "inst_id": "SOL-USDT-SWAP", "side": "buy", "posSide": "long",
            "sz": "1", "px": "100", "pnl": "0", "ordId": "1", "algoId": None, "state": "filled", "accFillSz": "1", "avgPx": "100"}
    W.handle(dict(fill), st, notifier=lambda e: sent.append(e) or True)
    # 重连补推：同一 ordId、不同 tradeId（key 不同）→ 不再发
    W.handle({**fill, "key": "ord:1:filled:t1-replay"}, st, notifier=lambda e: sent.append(e) or True)
    assert len(sent) == 1
    # 真正新的一笔仍发
    W.handle({**fill, "key": "ord:2:filled:t2", "ordId": "2"}, st, notifier=lambda e: sent.append(e) or True)
    assert len(sent) == 2


def test_failed_send_releases_fill_claim(tmp_path):
    st = W.State(path=tmp_path / "seen.json")
    fill = {"kind": "fill", "key": "ord:9:filled:a", "inst_id": "X-USDT-SWAP", "side": "buy", "posSide": "long",
            "sz": "1", "px": "1", "pnl": "0", "ordId": "9", "algoId": None, "state": "filled", "accFillSz": "1", "avgPx": "1"}
    W.handle(dict(fill), st, notifier=lambda e: False)
    sent = []
    W.handle({**fill, "key": "ord:9:filled:b"}, st, notifier=lambda e: sent.append(e) or True)
    assert len(sent) == 1


def test_position_replay_skipped_but_new_open_sent(tmp_path):
    p = tmp_path / "seen.json"
    sent = []
    nt = lambda e: sent.append(e) or True
    W.sync_snapshot([], "base", notifier=nt, path=p)          # 建基线
    old = W.PROC_START_MS - 3600_000
    W.ws_position(_pos("P1", old), path=p, notifier=nt, grace=0)
    assert [e["kind"] for e in sent] == ["position_open"]
    # 断线后快照里没了，又补推同一 pos_id 的旧开仓（cTime 字段变化）→ 已提醒过，不重发
    W.ws_position(_pos("P1", old, pos="0"), path=p, notifier=nt, grace=0)
    n = len(sent)
    W.ws_position(_pos("P1", old + 1), path=p, notifier=nt, grace=0)
    assert len(sent) == n
    # 补查发现的没提醒过的旧开仓仍提醒
    W.ws_position(_pos("P2", old), path=p, notifier=nt, grace=0)
    assert sent[-1]["pos_id"] == "P2" and len(sent) == n + 1
    # 真新开仓即时提醒
    W.ws_position(_pos("P3", int(time.time() * 1000)), path=p, notifier=nt, grace=0)
    assert sent[-1]["pos_id"] == "P3"


def test_alert_cooldown(tmp_path):
    p = tmp_path / "cd.json"
    t = 1_000_000.0
    assert W.cooldown_claim("healthcheck_stale", now=t, hours=6, path=p)
    assert not W.cooldown_claim("healthcheck_stale", now=t + 5 * 3600, hours=6, path=p)
    assert W.cooldown_claim("evening_missing", now=t + 5 * 3600, hours=6, path=p)   # 不同 event_type 独立
    assert W.cooldown_claim("healthcheck_stale", now=t + 6 * 3600 + 1, hours=6, path=p)
    for k in ("stop_loss", "fill", "position_open", "trigger_placed", "take_profit"):   # 交易类永不冷却
        assert W.cooldown_claim(k, now=t, hours=6, path=p) and W.cooldown_claim(k, now=t + 1, hours=6, path=p)


def test_notify_applies_cooldown(tmp_path, monkeypatch):
    import requests
    posts = []

    class R:
        ok, status_code = True, 200
    monkeypatch.setattr(requests, "post", lambda *a, **k: posts.append(k["data"]) or R())
    monkeypatch.delenv("TA_NOTIFY_DISABLED", raising=False)
    monkeypatch.setattr(W, "_notify_cfg", lambda: ("http://example.invalid/hook", ""))
    monkeypatch.setattr(W, "COOLDOWN_PATH", tmp_path / "cd.json")
    assert W.notify({"kind": "stale_code", "message": "a"})
    assert not W.notify({"kind": "stale_code", "message": "b"})
    assert W.notify({"kind": "stop_loss", "message": "s"}) and W.notify({"kind": "stop_loss", "message": "s"})
    assert len(posts) == 3
    assert b'"event_type": "stale_code"' in posts[0]   # webhook 格式不变


def test_stale_code_branch_condition(monkeypatch):
    monkeypatch.setattr(W, "_code_rev", {"start": "a" * 40, "alerted": 0.0})
    monkeypatch.setattr(W, "run_branch", lambda: "chore/publish-prep")
    sent = []
    nt = lambda e: sent.append(e) or True
    assert not W.check_code_version(now=1e9, head="b" * 40, notifier=nt, branch="feat/webhook-filter")
    assert not W.check_code_version(now=1e9, head="b" * 40, notifier=nt, branch="HEAD")   # detached
    assert not W.check_code_version(now=1e9, head="a" * 40, notifier=nt, branch="chore/publish-prep")
    assert sent == []
    assert W.check_code_version(now=1e9, head="b" * 40, notifier=nt, branch="chore/publish-prep")
    assert [e["kind"] for e in sent] == ["stale_code"]
