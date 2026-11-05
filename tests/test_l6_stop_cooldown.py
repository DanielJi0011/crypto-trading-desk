"""L6：止损记录与同向冷却机制（blocked 逻辑保留）；生产配置 2026-11-05 起为披露制（stop_cooldown_hours=0，硬拦截关闭）。"""
import time

from desk import settings, stop_cooldown
import pytest

from tests.test_tm_vote import demo  # noqa: F401  复用打桩夹具（不下真实单）


@pytest.fixture(autouse=True)
def _own_file(monkeypatch, tmp_path):   # 冷却记录只放本用例临时目录，不影响其他测试
    monkeypatch.setattr(stop_cooldown, "PATH", tmp_path / "stop_cooldown.json")


def test_record_and_block():
    cfg = {"risk": {"stop_cooldown_hours": 24}}   # blocked() 机制本身不变，用显式配置验证
    assert stop_cooldown.hours(cfg) == 24
    now = time.time()
    assert stop_cooldown.record("BTC-USDT-SWAP", "sell", "net", "A1", now=now - 3600) == "long"
    assert stop_cooldown.blocked(cfg, "BTC-USDT-SWAP", "long", now=now)["left_h"] == 23.0
    assert stop_cooldown.blocked(cfg, "BTC-USDT-SWAP", "short", now=now) is None        # 反向不限
    assert stop_cooldown.blocked(cfg, "BTC-USDT-SWAP", "long", now=now + 24 * 3600) is None   # 过期
    assert stop_cooldown.record("ETH-USDT-SWAP", "buy", "short") == "short"
    assert stop_cooldown.record("ETH-USDT-SWAP", None, None) is None
    # 生产配置（2026-11-05 用户决定）：硬冷却关闭，blocked() 恒为 None，改披露制
    prod = settings.load_config()
    assert stop_cooldown.hours(prod) == 0
    assert stop_cooldown.blocked(prod, "BTC-USDT-SWAP", "long", now=now) is None


def test_pm_no_longer_blocks_same_direction(demo):  # noqa: F811
    """2026-11-05 披露制：刚止损过同向也不再硬拦截，交易经理的开仓照常执行（是否重开由它知情后决定）。"""
    cfg, manager, calls, st, bk = demo
    inst = cfg["coins"][0]["inst_id"]
    from desk import okx_demo as X
    X._inst_cache.setdefault(inst, {"ctVal": "0.01", "lotSz": "0.01", "minSz": "0.01", "tickSz": "0.1"})   # 离线定张数
    stop_cooldown.record(inst, "buy", "short")      # 空单刚被止损；夹具里各角色看空
    from tests._tool_tm import from_side
    res = manager.pm_execute_demo(cfg, cfg["coins"][0], "D-C", st, manager.weights(), {"last": 100.0}, bk, None, "t",
                                  tm=from_side("short", entry=100.0), tm_state="go")
    assert calls["open_now"] + calls["place_trigger"] >= 1 and res["action"] != "hold(止损冷却)"


def test_describe_disclosure():
    now = time.time()
    assert stop_cooldown.describe("BTC-USDT-SWAP", "short", now=now) is None      # 无记录不披露
    stop_cooldown.record("ETH-USDT-SWAP", "buy", "short", "ALGO9", now=now - 2 * 3600)
    d = stop_cooldown.describe("ETH-USDT-SWAP", "short", now=now)
    assert "最近一次止损" in d and "空单" in d and "约 2.0 小时前" in d and "ALGO9" in d
    assert "止损后同向重开" in d and "由你自行决定" in d and "无法区分是哪台机器" in d
    d2 = stop_cooldown.describe("ETH-USDT-SWAP", "long", now=now)                  # 反向共识不点名重开
    assert "止损后同向重开" not in d2
    r = stop_cooldown.last_record("ETH-USDT-SWAP", now=now)
    assert r["side"] == "short" and r["age_h"] == 2.0


def test_prompt_discloses_stop_and_cadence():
    from desk import manager
    stop_cooldown.record("ETH-USDT-SWAP", "buy", "short", "ALGO9", now=time.time() - 3600)
    cfg = {"guardrails": {"max_position_pct": 10, "max_total_notional_pct": 100, "max_stop_loss_pct": 8,
                          "max_daily_loss_pct": 3},
           "manager": {}}
    mk = {"last": 2500.0, "close": 2500.0, "bar_date": "d", "ret_5d": 0, "ret_20d": 0, "atr14": 50.0, "hi_20": 1, "lo_20": 1}
    bk = {"cash": 1000.0, "equity": 1000.0, "exposure": 0.0, "positions": [], "triggers": []}
    st = {r: {"direction": "hold"} for r in manager.ROLE_KEYS}
    texts = {"portfolio_manager": "", "trader": ""}
    p = manager._prompt(cfg, {"name": "ETH", "inst_id": "ETH-USDT-SWAP"}, mk, None, bk, st, {}, -0.3, texts)
    assert "## 近期止损披露" in p and "止损后同向重开" in p
    assert "## 决策节奏（固定时间决策）" in p and "不是连续盯盘" in p


def test_ws_records_stop(monkeypatch):
    import desk.demo_ws as dw
    src = open(dw.__file__, encoding="utf-8").read()
    assert "stop_cooldown.record(ev.get(\"inst_id\"), ev.get(\"side\"), ev.get(\"posSide\")" in src
