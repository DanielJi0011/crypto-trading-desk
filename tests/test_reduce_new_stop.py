"""减仓 + new_stop：收紧生效且止损单张数=剩余仓位；放宽被拒并记录；无 new_stop 行为不变。离线桩，不碰真实 state，不发网络请求。"""
from types import SimpleNamespace

import pytest

from desk import demo_exec as DX, manager, settings


@pytest.fixture
def fake(monkeypatch):
    calls = {"post": [], "algos": [{"algoId": "A1", "posSide": "net", "slTriggerPx": "100", "tpTriggerPx": "150"}]}
    monkeypatch.setattr(DX, "_require", lambda: None)
    monkeypatch.setattr(DX, "close_algos", lambda inst: calls["algos"])
    def post(path, body):
        calls["post"].append((path, body))
        if path.endswith("amend-algos"):
            raise DX.X.DemoError("51506 not supported")       # 走"先挂新再撤旧"路径，检验张数
        if path.endswith("order-algo"):
            calls["algos"] = calls["algos"] + [{"algoId": "A2", "posSide": "net", "slTriggerPx": body["slTriggerPx"]}]
            return {"data": [{"algoId": "A2"}]}
        return {"data": [{}]}
    monkeypatch.setattr(DX.X, "post", post)
    monkeypatch.setattr(DX.X, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(DX.X, "_px", lambda inst, px: str(px))
    monkeypatch.setattr(DX, "_alert", lambda *a, **k: None)
    return calls


POS = {"inst_id": "SOL-USDT-SWAP", "venue": "okx_demo", "side": "long", "pos_side": "net", "contracts": 10.0,
       "sl_px": 100.0, "tp_px": 150.0, "price": 120.0, "trade_id": "T1"}


def _dec(new_stop):
    return SimpleNamespace(new_stop=new_stop)


def test_reduce_tighten_applies_with_remaining_size(fake):
    notes = []
    manager.apply_new_stop(settings.load_config(), POS, _dec(110.0), "reduce", {"type": "reduce", "ok": True, "remaining": 4.0}, notes)
    placed = [b for p, b in fake["post"] if p.endswith("order-algo")]
    assert len(placed) == 1 and placed[0]["sz"] == DX.X._dec(4.0) and placed[0]["slTriggerPx"] == "110.0"
    assert placed[0]["reduceOnly"] is True
    assert any("cancel-algos" in p for p, _ in fake["post"])        # 旧单撤掉
    assert notes and "110" in notes[-1]


def test_reduce_widen_rejected_and_noted(fake):
    notes = []
    cfg = settings.load_config(); cfg["manager"]["allow_stop_widen"] = False
    manager.apply_new_stop(cfg, POS, _dec(90.0), "reduce", {"type": "reduce", "ok": True, "remaining": 4.0}, notes)
    assert fake["post"] == []
    assert notes and "禁止放宽止损" in notes[-1]


def test_short_reduce_widen_rejected(fake):
    notes = []
    cfg = settings.load_config(); cfg["manager"]["allow_stop_widen"] = False
    p = {**POS, "side": "short", "sl_px": 130.0}
    manager.apply_new_stop(cfg, p, _dec(140.0), "reduce", {"ok": True, "remaining": 4.0}, notes)
    assert fake["post"] == [] and "禁止放宽止损" in notes[-1]


def test_reduce_without_new_stop_unchanged(fake):
    notes = []
    manager.apply_new_stop(settings.load_config(), POS, _dec(None), "reduce", {"ok": True, "remaining": 4.0}, notes)
    assert fake["post"] == [] and notes == []


def test_reduce_failed_does_not_move_stop(fake):
    notes = []
    manager.apply_new_stop(settings.load_config(), POS, _dec(110.0), "reduce", {"ok": False}, notes)
    assert fake["post"] == [] and notes == ["减仓未成功，new_stop 未执行"]


def test_reduce_reports_remaining(monkeypatch, fake):
    monkeypatch.setattr(DX.X, "instrument", lambda inst: {"lotSz": "1"})
    monkeypatch.setattr(DX, "_replace_close_algos", lambda *a: None)
    r = DX._reduce(settings.load_config(), {**POS, "upnl": 0.0}, 0.6, "t")
    assert r["ok"] and r["remaining"] == 4.0
