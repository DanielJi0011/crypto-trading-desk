"""事故回归：测试中任何 webhook/网络 POST 都必须失败，通知必须因无地址而不发送。"""
import pytest
import requests

from desk import demo_ws as W, equity_guard as EG


def test_requests_post_forbidden():
    with pytest.raises(AssertionError):
        requests.post("https://example.invalid", data=b"x")
    with pytest.raises(AssertionError):
        requests.request("POST", "https://example.invalid")


def test_notify_has_no_url():
    assert W._notify_cfg() == ("", "")
    assert W.notify({"kind": "daily_loss_halt", "message": "t"}) is False


def test_equity_guard_default_notifier_does_not_send(tmp_path, monkeypatch):
    monkeypatch.setattr(EG, "MARKS", tmp_path / "m.json")
    EG._notify("daily_loss_halt", "t")   # 无地址 → 只写本地日志
