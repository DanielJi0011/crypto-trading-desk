"""挂单提醒：trigger_placed / entry_limit_registered 同一 algoId 只提醒一次；复用 demo_ws.notify 通道（不连网、不碰真实 state）。"""
import pytest

from desk import demo_exec as DX, demo_ws as W, entry_limit as EL, okx_demo as X, settings

INST = "ETH-USDT-SWAP"
INSTR = {"ctVal": "0.1", "lotSz": "0.01", "minSz": "0.01", "tickSz": "0.01"}


def META(aid="A1", source="daily", **kw):
    return {"inst_id": INST, "algoId": aid, "side": "long", "trigger_px": 2500.5, "sz": "12.3", "stop_loss": 2400,
            "take_profit": 2700, "decision_id": "D-1", "source": source, "order_px": "-1", **kw}


@pytest.fixture
def seen(tmp_path, monkeypatch):
    p = tmp_path / "placed.json"
    monkeypatch.setattr(W, "PLACED_SEEN", p)
    monkeypatch.setattr(W, "LOG", tmp_path / "ws.log", raising=False)
    return p


def test_message_fields_chinese(seen, monkeypatch):
    monkeypatch.setattr(settings, "SESSION", "morning")
    sent = []
    assert W.notify_placed(META(), notifier=lambda e: sent.append(e) or True)
    e = sent[0]; m = e["message"]
    assert e["kind"] == "trigger_placed" and e["kind"] in W.NOTIFY_KINDS
    for s in ("ETH", "做多", "2500.5", "12.3 张", "止损 2400", "止盈 2700", "D-1", "8点早场"):
        assert s in m
    p = W.payload(e)
    assert p["event_type"] == "trigger_placed" and p["trigger_price"] == 2500.5 and p["stop_loss"] == 2400
    assert p["take_profit"] == 2700 and p["decision_id"] == "D-1" and p["session"] == "8点早场" and p["algo_id"] == "A1"


@pytest.mark.parametrize("source,session,label", [("daily", "morning", "8点早场"), ("daily", "evening", "20:30晚场"),
                                                   ("post_stop:DEMO-9", "morning", "止损后再讨论"),
                                                   ("post_tp:DEMO-9", "evening", "止盈后再讨论")])
def test_session_labels(seen, monkeypatch, source, session, label):
    monkeypatch.setattr(settings, "SESSION", session)
    assert W.session_label(source) == label
    assert f"来源 {label}" in W.placed_event(META(source=source))["message"]


def test_dedup_by_algoid_and_retry_after_failure(seen):
    sent = []
    ok = lambda e: sent.append(e) or True
    assert W.notify_placed(META("A1"), notifier=ok)
    assert not W.notify_placed(META("A1"), notifier=ok)        # entry_limit_registered / 重复调用不再发
    assert W.notify_placed(META("A2"), notifier=ok)
    assert [e["algoId"] for e in sent] == ["A1", "A2"]
    assert not W.notify_placed(META("B1"), notifier=lambda e: False)   # 发送失败释放
    assert W.notify_placed(META("B1"), notifier=ok)
    assert not W.notify_placed(META(None), notifier=ok)


def test_notifier_exception_swallowed(seen):
    def boom(e):
        raise RuntimeError("x")
    assert W.notify_placed(META(), notifier=boom) is False


def test_real_notify_uses_same_channel_without_network(seen, monkeypatch, tmp_path):
    """走真实 notify()：读 TA_NOTIFY_WEBHOOK_URL/KEY；requests.post 被替换为本地捕获，不发网络。日志不含 token。"""
    import requests
    monkeypatch.delenv("TA_NOTIFY_DISABLED", raising=False)
    monkeypatch.setenv("TA_NOTIFY_WEBHOOK_URL", "http://127.0.0.1:9/fake-hook")
    monkeypatch.setenv("TA_NOTIFY_WEBHOOK_KEY", "TESTKEY-SHOULD-NOT-LEAK")
    logs, calls = [], []
    monkeypatch.setattr(W, "log", logs.append)

    class R:
        ok, status_code = True, 200
    monkeypatch.setattr(requests, "post", lambda url, data=None, headers=None, timeout=None: calls.append((url, data)) or R())
    assert W.notify_placed(META("R1"))
    assert not W.notify_placed(META("R1"))
    assert len(calls) == 1 and "挂单提醒".encode() in calls[0][1]
    assert not any("TESTKEY" in s or "fake-hook" in s for s in logs)


def test_disabled_switch_sends_nothing(seen, monkeypatch):
    monkeypatch.setenv("TA_NOTIFY_DISABLED", "1")
    assert W.notify_placed(META("Q1")) is False


def test_submit_trigger_limit_notifies_once(seen, tmp_path, monkeypatch):
    """限价入场：trigger_placed 与 entry_limit_registered 两个事件，同一 algoId 只提醒一次。"""
    monkeypatch.setattr(EL, "PATH", tmp_path / "el.json")
    monkeypatch.setattr(EL, "LOCK", tmp_path / "el.lock")
    monkeypatch.setattr(X, "EVENTS", tmp_path / "ev.jsonl")
    from desk import entry_state
    monkeypatch.setattr(entry_state, "advance", lambda *a, **k: None)
    monkeypatch.setattr(DX, "_require", lambda: None)
    monkeypatch.setattr(DX, "_ps", lambda side: "net")
    monkeypatch.setattr(DX, "_set_lev", lambda *a: None)
    monkeypatch.setattr(DX, "save_reg", lambda r: None)
    monkeypatch.setattr(DX, "reg", lambda: {"triggers": {}})
    monkeypatch.setattr(X, "instrument", lambda inst: INSTR)
    monkeypatch.setattr(X, "post", lambda p, b: {"data": [{"algoId": "Z9"}]})
    sent = []
    monkeypatch.setattr(W, "notify", lambda e: sent.append(e) or True)
    monkeypatch.setattr(settings, "SESSION", "evening")
    cfg = settings.load_config()
    cfg["execution"]["entry_limit"] = {**cfg["execution"].get("entry_limit", {}), "enabled": True, "offset_pct": 0.1}
    o = {"side": "short", "trigger_px": 2500, "leverage": 2, "stop_loss": 2600, "take_profit": 2300,
         "decision_id": "D-9", "source": "daily"}
    DX.submit_trigger(cfg, INST, o, "5", "2026-10-25 00:00:00+0800")
    DX.submit_trigger(cfg, INST, o, "5", "2026-10-25 00:00:00+0800")   # 同 algoId 再次（模拟重复）
    evs = [l for l in (tmp_path / "ev.jsonl").read_text().splitlines()]
    assert any("trigger_placed" in l for l in evs) and any("entry_limit_registered" in l for l in evs)
    assert len(sent) == 1 and "做空" in sent[0]["message"] and "20:30晚场" in sent[0]["message"] and "D-9" in sent[0]["message"]
