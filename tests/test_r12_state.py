"""R12 入场状态机：合法路径、非法跳转拒绝、demo_watch 按子单状态推进、旧登记不补记。离线。"""
import pytest

from desk import demo_watch as W, entry_state as ES, okx_demo as X, settings


@pytest.fixture(autouse=True)
def _tmp(monkeypatch, tmp_path):
    monkeypatch.setattr(ES, "PATH", tmp_path / "es.json")


def _ev(kind):
    return [e for e in settings.read_jsonl(X.EVENTS) if e["event"] == kind]


def test_trigger_path():
    for s in ("armed", "triggered", "partial", "partial", "filled", "closed"):
        assert ES.advance("A1", s)
    assert ES.get("A1") == "closed"
    assert [h[1] for h in ES.load()["A1"]["history"]] == ["armed", "triggered", "partial", "partial", "filled", "closed"]


def test_illegal_rejected():
    ES.advance("A2", "armed")
    n = len(_ev("entry_state_illegal"))
    assert not ES.advance("A2", "filled")          # 未触发不能直接成交
    assert not ES.advance("A2", "closed")
    assert ES.get("A2") == "armed" and len(_ev("entry_state_illegal")) == n + 2
    ES.advance("A2", "expired")
    assert not ES.advance("A2", "triggered")       # 终态不可再动


def test_open_path():
    assert ES.advance("O1", "open_sent") and ES.advance("O1", "filled") and ES.advance("O1", "closed")


def _order(monkeypatch, **o):
    monkeypatch.setattr(X, "get", lambda path, **kw: {"data": [o]})


def test_watch_advances_by_child_state(monkeypatch):
    ES.advance("AW", "armed"); ES.advance("AW", "triggered")
    m = {"from_algo": "AW", "child_ordId": "C1", "horizon_days": 1}
    _order(monkeypatch, state="partially_filled", accFillSz="1", fillTime="1761184800000")
    W._entry_filled("BTC-USDT-SWAP", m)
    assert ES.get("AW") == "partial"
    _order(monkeypatch, state="canceled", accFillSz="1", avgPx="100")
    W._entry_filled("BTC-USDT-SWAP", m)
    assert ES.get("AW") == "filled"


def test_watch_child_canceled_no_fill(monkeypatch):
    ES.advance("AC", "armed"); ES.advance("AC", "triggered")
    _order(monkeypatch, state="canceled", accFillSz="0")
    W._entry_filled("BTC-USDT-SWAP", {"from_algo": "AC", "child_ordId": "C2"})
    assert ES.get("AC") == "canceled"


def test_legacy_untracked_not_spammed(monkeypatch):
    n = len(_ev("entry_state_illegal"))
    _order(monkeypatch, state="filled", accFillSz="5")
    W._entry_filled("ETH-USDT-SWAP", {"child_ordId": "OLD"})
    assert ES.get("OLD") is None and len(_ev("entry_state_illegal")) == n
