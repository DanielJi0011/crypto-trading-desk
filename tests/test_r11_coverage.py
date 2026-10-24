"""R11 止损覆盖：缺失连续两轮确认后告警；默认只告警不下单；开启自动重挂后按最近已知止损先挂新。离线。"""
import pytest

from desk import settings, stop_coverage as SC
from tests.fake_okx import FakeOKX

P = {"inst_id": "ETH-USDT-SWAP", "side": "short", "pos_side": "short", "contracts": 46.18, "price": 2390.0, "venue": "okx_demo",
     "sl_px": None, "tp_px": None}
OCO = {"algoId": "OLD", "instId": "ETH-USDT-SWAP", "ordType": "oco", "posSide": "short", "side": "buy", "sz": "46.18",
       "slTriggerPx": "2510", "tpTriggerPx": "2315.95", "reduceOnly": True}


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(SC, "PATH", tmp_path / "sc.json")
    f = FakeOKX().install(monkeypatch)
    return f


def _cfg(auto):
    cfg = settings.load_config()
    cfg.setdefault("execution", {})["stop_coverage_auto_replace"] = auto
    return cfg


def _ev():
    from desk import okx_demo as X
    return [e for e in settings.read_jsonl(X.EVENTS)]


def test_covered_remembers_last_known(env):
    env.algos.append(dict(OCO))
    assert SC.check(_cfg(False), {"positions": [dict(P)]}) == [("ETH-USDT-SWAP:short", "ok")]
    assert SC._load()["last_known"]["ETH-USDT-SWAP:short"]["sl"] == 2510.0


def test_missing_alert_only_by_default(env):
    env.algos.append(dict(OCO)); SC.check(_cfg(False), {"positions": [dict(P)]})
    env.algos.clear()
    assert SC.check(_cfg(False), {"positions": [dict(P)]})[0][1] == "pending_confirm"
    assert SC.check(_cfg(False), {"positions": [dict(P)]})[0][1] == "missing"
    assert SC.check(_cfg(False), {"positions": [dict(P)]})[0][1] == "missing"
    assert [e["event"] for e in _ev()].count("stop_coverage_missing") == 1      # 同一缺口只告警一次
    assert not [c for c in env.calls if c[0] == "POST"]                         # 默认不下单


def test_auto_replace_uses_last_known(env):
    env.algos.append(dict(OCO)); SC.check(_cfg(True), {"positions": [dict(P)]})
    env.algos.clear()
    SC.check(_cfg(True), {"positions": [dict(P)]})
    assert SC.check(_cfg(True), {"positions": [dict(P)]})[0][1] == "replaced"
    new = [c[2] for c in env.calls if c[0] == "POST" and c[1].endswith("order-algo")][0]
    assert new["slTriggerPx"] == "2510.00" and new["tpTriggerPx"] == "2315.95" and new["reduceOnly"] is True
    assert new["sz"] == "46.18" and new["side"] == "buy"
    env.calls.clear()
    assert SC.check(_cfg(True), {"positions": [dict(P)]})[0][1] == "ok"
    assert "stop_coverage_restored" in [e["event"] for e in _ev()]


def test_auto_replace_partial_size(env):
    env.algos.append(dict(OCO, sz="20"))
    SC.check(_cfg(True), {"positions": [dict(P)]})
    assert SC.check(_cfg(True), {"positions": [dict(P)]})[0][1] == "replaced"
    assert len(env.algos) == 1 and env.algos[0]["sz"] == "46.18" and env.algos[0]["algoId"] != "OLD"


def test_halt_never_places(env):
    SC.check(_cfg(True), {"positions": [dict(P)]}, halt=True)
    assert SC.check(_cfg(True), {"positions": [dict(P)]}, halt=True)[0][1] == "missing"
    assert not [c for c in env.calls if c[0] == "POST"]


def test_only_isolates_other_positions(env):
    other = dict(P, inst_id="DOGE-USDT-SWAP", side="long", pos_side="long", contracts=1, price=0.2)
    bk = {"positions": [dict(P), other]}
    SC.check(_cfg(True), bk, only={"DOGE-USDT-SWAP"}, source="test")
    res = SC.check(_cfg(True), bk, only={"DOGE-USDT-SWAP"}, source="test")
    assert [k for k, _ in res] == ["DOGE-USDT-SWAP:long"]
    posts = [c[2] for c in env.calls if c[0] == "POST" and c[1].endswith("order-algo")]
    assert posts and all(b["instId"] == "DOGE-USDT-SWAP" for b in posts)
    assert all(e.get("source") == "test" for e in _ev() if e["event"].startswith("stop_coverage"))
