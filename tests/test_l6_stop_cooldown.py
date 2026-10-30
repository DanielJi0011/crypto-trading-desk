"""L6：止损后同向冷却 24 小时。"""
import time

from desk import settings, stop_cooldown
import pytest

from tests.test_tm_vote import demo  # noqa: F401  复用打桩夹具（不下真实单）


@pytest.fixture(autouse=True)
def _own_file(monkeypatch, tmp_path):   # 冷却记录只放本用例临时目录，不影响其他测试
    monkeypatch.setattr(stop_cooldown, "PATH", tmp_path / "stop_cooldown.json")


def test_record_and_block():
    cfg = settings.load_config()
    assert stop_cooldown.hours(cfg) == 24
    now = time.time()
    assert stop_cooldown.record("BTC-USDT-SWAP", "sell", "net", "A1", now=now - 3600) == "long"
    assert stop_cooldown.blocked(cfg, "BTC-USDT-SWAP", "long", now=now)["left_h"] == 23.0
    assert stop_cooldown.blocked(cfg, "BTC-USDT-SWAP", "short", now=now) is None        # 反向不限
    assert stop_cooldown.blocked(cfg, "BTC-USDT-SWAP", "long", now=now + 24 * 3600) is None   # 过期
    assert stop_cooldown.record("ETH-USDT-SWAP", "buy", "short") == "short"
    assert stop_cooldown.record("ETH-USDT-SWAP", None, None) is None


def test_pm_blocks_same_direction(demo):  # noqa: F811
    cfg, manager, calls, st, bk = demo
    inst = cfg["coins"][0]["inst_id"]
    stop_cooldown.record(inst, "buy", "short")      # 空单刚被止损；夹具里各角色看空
    cfg["manager"]["tm_required_for_open"] = False   # 本测试验证止损冷却，沿用无交易经理点位的旧回退
    res = manager.pm_execute_demo(cfg, cfg["coins"][0], "D-C", st, manager.weights(), {"last": 100.0}, bk, None, "t",
                                  tm=None, tm_state="go")
    assert calls["open_now"] == 0 and calls["place_trigger"] == 0 and res["action"] == "hold(止损冷却)"
    assert any("止损冷却" in n for n in res["notes"])
    ev = [e for e in settings.read_jsonl(settings.STATE / "ledger" / "demo_events.jsonl") if e["event"] == "cooldown_blocked"]
    assert ev and ev[-1]["inst_id"] == inst


def test_ws_records_stop(monkeypatch):
    import desk.demo_ws as dw
    src = open(dw.__file__, encoding="utf-8").read()
    assert "stop_cooldown.record(ev.get(\"inst_id\"), ev.get(\"side\"), ev.get(\"posSide\")" in src
