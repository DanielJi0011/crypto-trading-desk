"""B2：收紧止损遇 51506 → 先挂新再撤旧；新单失败保留旧单并告警；只能收紧。"""
import pytest

from desk import settings
from tests.fake_okx import FakeOKX

ETH = {"inst_id": "ETH-USDT-SWAP", "side": "short", "pos_side": "short", "contracts": 46.18, "sl_px": 2603.31,
       "tp_px": 2315.95, "price": 2390.0}


def _setup(monkeypatch):
    f = FakeOKX().install(monkeypatch)
    f.algos.append({"algoId": "OLD", "instId": "ETH-USDT-SWAP", "ordType": "oco", "posSide": "short", "side": "buy",
                    "sz": "46.18", "slTriggerPx": "2603.31", "tpTriggerPx": "2315.95", "reduceOnly": True})
    return f


def _events():
    from desk import okx_demo as X
    return [e["event"] for e in settings.read_jsonl(X.EVENTS)]


def test_51506_place_new_then_cancel_old(monkeypatch):
    from desk import demo_exec as DX
    f = _setup(monkeypatch); f.amend_code = "51506"
    ok, msg = DX.move_stop(settings.load_config(), dict(ETH), 2510.0)
    assert ok, msg
    posts = [c for c in f.calls if c[0] == "POST"]
    order = [c[1].rsplit("/", 1)[1] for c in posts]
    assert order == ["amend-algos", "order-algo", "cancel-algos"]           # 先挂新，后撤旧
    new = posts[1][2]
    assert new["slTriggerPx"] == "2510.00" and new["tpTriggerPx"] == "2315.95"  # 保留原止盈
    assert new["reduceOnly"] is True and new["sz"] == "46.18" and new["side"] == "buy" and new["posSide"] == "short"
    assert [a["algoId"] for a in f.algos] != ["OLD"] and len(f.algos) == 1
    assert "stop_amend_unsupported" in _events() and "stop_ok" in _events()


def test_new_order_failure_keeps_old_and_alerts(monkeypatch):
    from desk import demo_exec as DX, okx_demo as X
    f = _setup(monkeypatch); f.amend_code = "51506"
    f.fail["/api/v5/trade/order-algo"] = X.DemoError("boom")
    sent = []
    monkeypatch.setattr("desk.demo_ws.notify", lambda ev: sent.append(ev["kind"]) or True)
    ok, _ = DX.move_stop(settings.load_config(), dict(ETH), 2510.0)
    assert not ok and [a["algoId"] for a in f.algos] == ["OLD"]
    assert not any(c[1].endswith("cancel-algos") for c in f.calls)
    assert sent == ["stop_replace_failed"] and "stop_replace_failed" in _events()


def test_new_order_not_visible_keeps_old(monkeypatch):
    from desk import demo_exec as DX
    f = _setup(monkeypatch); f.amend_code = "51506"
    real = f.post

    def ghost(path, body):
        r = real(path, body)
        if path.endswith("order-algo"):
            f.algos.pop()   # 交易所回成功但查不到
        return r
    monkeypatch.setattr("desk.okx_demo.post", ghost)
    ok, msg = DX.move_stop(settings.load_config(), dict(ETH), 2510.0)
    assert not ok and [a["algoId"] for a in f.algos] == ["OLD"] and "未确认" in msg


@pytest.mark.parametrize("new_sl", [2650.0, 2603.31])
def test_only_tighten(monkeypatch, new_sl):
    from desk import demo_exec as DX
    f = _setup(monkeypatch); f.amend_code = "51506"
    ok, _ = DX.tighten_by_replace(settings.load_config(), dict(ETH), new_sl)
    assert not ok and not any(c[0] == "POST" for c in f.calls)


def test_stop_beyond_price_rejected(monkeypatch):
    from desk import demo_exec as DX
    f = _setup(monkeypatch)
    ok, _ = DX.tighten_by_replace(settings.load_config(), dict(ETH), 2380.0)
    assert not ok and not any(c[0] == "POST" for c in f.calls)


def test_amend_supported_still_amends(monkeypatch):
    from desk import demo_exec as DX
    f = _setup(monkeypatch)
    ok, _ = DX.move_stop(settings.load_config(), dict(ETH), 2510.0)
    assert ok and f.algos[0]["algoId"] == "OLD" and f.algos[0]["slTriggerPx"] == "2510.00"


def test_no_stop_algo_places_without_cancel_first(monkeypatch):
    from desk import demo_exec as DX
    f = FakeOKX().install(monkeypatch)
    ok, _ = DX.move_stop(settings.load_config(), dict(ETH), 2510.0)
    assert ok and [c[1].rsplit("/", 1)[1] for c in f.calls if c[0] == "POST"] == ["order-algo"]
