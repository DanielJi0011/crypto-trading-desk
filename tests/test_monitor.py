"""离线测试：再讨论调度（限频/在途/续跑）、条件单替换、聚合与 pm_execute 挂单、纸面账本盯市数学。
（纸面监控 process_pending/run_once 的触发/confirm 测试已随该引擎于 2026-11-15 退休删除；
schedule_rediscuss 的止损触发语义另由 test_demo_ws.py 覆盖。）"""
import datetime as dt

import pandas as pd
import pytest

from desk import settings


@pytest.fixture()
def env(monkeypatch):
    from desk import ledger, monitor, pending
    for p in (ledger.store.TRADES_CSV, ledger.store.ORDERS_LOG, ledger.ACCOUNT, ledger.META, pending.PENDING,
              pending.EVENTS, monitor.REDISCUSS):
        if p.exists():
            p.unlink()
    cfg = settings.load_config()
    kp = ledger.kill_switch_path(cfg)
    if kp.exists():
        kp.unlink()
    monkeypatch.setattr(ledger, "PRICE", lambda inst: 100.0)
    monkeypatch.setattr(ledger, "FUNDING", lambda inst: 0.0001)
    calls = []
    monkeypatch.setattr(monitor, "SPAWN", lambda args, lf: calls.append(args))
    return cfg, ledger, monitor, pending, calls


def bars(rows, start_ms):
    return pd.DataFrame([{"ts": start_ms + i * 60_000, "Open": o, "High": h, "Low": l, "Close": c}
                         for i, (o, h, l, c) in enumerate(rows)])


def order(**kw):
    o = {"decision_id": "D-T", "source": "test", "side": "long", "leverage": 1, "trigger_px": 105.0, "condition": "gte",
         "stop_loss": 100.0, "take_profit": 120.0, "size_pct": 10, "horizon_days": 5, "expire_hours": 24,
         "consensus": 0.5, "ref_px": 100, "reason": "突破"}
    o.update(kw)
    return o


def test_short_tp_on_low_and_profit_sign(env, monkeypatch):
    cfg, ledger, *_ = env
    # 1x 下保证金 = 名义，单笔保证金上限 20%×1000=200，故名义从 300 改为 150
    ok, info = ledger.open_position(cfg, "SOL-USDT-SWAP", 150, 105.0, 90.0, 5, "D", "t", 1000, side="short", leverage=1)
    assert ok, info
    entry = 100 * 0.9995                                   # 空单卖出，滑点 5bp 对己方不利
    assert info["margin"] == pytest.approx(info["qty"] * entry)
    assert info["liq_px"] == pytest.approx(entry * (1 + 1 / 1 - 0.005))
    monkeypatch.setattr(ledger, "BARS", lambda inst: bars([(99, 100, 89, 92)], int(settings.now().timestamp() * 1000) + 60_000))
    c = ledger.mark_to_market(cfg)
    assert c[0]["exit_reason"] == "止盈" and float(c[0]["exit_px"]) == 90 and float(c[0]["pnl_usd"]) > 0
    # pnl = qty×(entry−90) − 平仓费 qty×90×0.0005 − 开仓费 qty×entry×0.0005 − 资金费（持仓≈0 小时，可忽略）
    q = info["qty"]
    assert float(c[0]["pnl_usd"]) == pytest.approx(q * (entry - 90) - q * 90 * 0.0005 - q * entry * 0.0005, rel=1e-4)


def test_liquidation_guard_and_mark(env, monkeypatch):
    cfg, ledger, *_ = env
    # 3x 多单强平价约 100*(1-1/3+0.005)=67.2；止损 60 在强平之后 → 拒单（另外止损距离也超限）
    # 本测试专门验证高杠杆强平保护（3x 场景，config 上限现为 5，3x 在范围内）。
    # Q11 后默认新开仓 1 倍；本用例显式放开到 5 倍（测试参数，不改 config）以验证强平保护
    cfg2 = {**cfg, "swap": {**cfg["swap"], "leverage_max": 5},
            "guardrails": {**cfg["guardrails"], "max_stop_loss_pct": 50, "leverage": 5, "allow_leverage": True}}
    ok, info = ledger.open_position(cfg2, "BTC-USDT-SWAP", 300, 60.0, None, 5, "D", "t", 1000, side="long", leverage=3)
    assert not ok and any("LIQUIDATION" in r for r in info["reasons"])
    # mark_to_market 跳空越过止损和强平价 → 强平，亏光保证金
    ok, info = ledger.open_position(cfg2, "BTC-USDT-SWAP", 300, 70.0, None, 5, "D", "t", 1000, side="long", leverage=3)
    assert ok, info
    monkeypatch.setattr(ledger, "BARS", lambda inst: bars([(50, 52, 49, 50)], int(settings.now().timestamp() * 1000) + 60_000))
    c = ledger.mark_to_market(cfg2)
    assert c[0]["exit_reason"] == "强平" and float(c[0]["pnl_usd"]) == pytest.approx(-info["margin"] - info["fee"], rel=1e-6)


def test_replace_one_per_coin(env):
    cfg, ledger, monitor, pending, _ = env
    pending.place(cfg, "BTC-USDT-SWAP", order())
    pending.place(cfg, "BTC-USDT-SWAP", order(trigger_px=110.0))
    assert len(pending.load()) == 1 and pending.load()["BTC-USDT-SWAP"]["trigger_px"] == 110.0
    assert "替换" in [e["type"] for e in pending.events()]


def test_rediscuss_stale_inflight_resumes_and_spawn_failure_retries(env, monkeypatch):
    cfg, ledger, monitor, pending, calls = env
    tr = {"inst_id": "ETH-USDT-SWAP", "trade_id": "DEMO-999", "reason": "sl"}
    assert monitor.schedule_rediscuss(cfg, tr) == "已启动" and len(calls) == 1
    # 在途未超时 → 不重复启动
    assert monitor.schedule_rediscuss(cfg, tr) == "再讨论进行中" and len(calls) == 1
    # 在途超时（模拟子进程被杀）→ 续跑
    st = settings.read_json(monitor.REDISCUSS, {})
    stale = (settings.now() - dt.timedelta(hours=monitor.REDISCUSS_STALE_HOURS + 1)).strftime("%Y-%m-%d %H:%M:%S%z")
    st["ETH-USDT-SWAP"]["inflight"]["DEMO-999"] = stale
    st["ETH-USDT-SWAP"]["last"] = stale   # 冷却也已过，避免续跑被冷却挡住
    settings.write_json(monitor.REDISCUSS, st)
    assert monitor.schedule_rediscuss(cfg, tr) == "已启动" and len(calls) == 2
    assert monitor.complete_rediscuss("ETH-USDT-SWAP", "DEMO-999") is True
    assert monitor.schedule_rediscuss(cfg, tr) == "该事件已讨论过"
    # 启动失败不留假在途 → 下次可重试
    tr3 = {"inst_id": "SOL-USDT-SWAP", "trade_id": "DEMO-1000", "reason": "tp"}
    monkeypatch.setattr(monitor, "SPAWN", lambda args, lf: (_ for _ in ()).throw(RuntimeError("boom")))
    assert monitor.schedule_rediscuss(cfg, tr3) == "启动失败"
    st = settings.read_json(monitor.REDISCUSS, {})
    assert "DEMO-1000" not in st["SOL-USDT-SWAP"].get("inflight", {})
    monkeypatch.setattr(monitor, "SPAWN", lambda args, lf: calls.append(args))
    assert monitor.schedule_rediscuss(cfg, tr3) == "已启动"


def test_aggregation_signed_and_pm_not_voting(env):
    cfg, ledger, monitor, pending, _ = env
    from desk import manager
    from desk.roles import ROLE_KEYS
    w = manager.weights()
    st = {r: {"direction": "bearish", "confidence": 0.8, "stop": 104.0, "target": 90.0, "entry_px": 98.0,
              "entry_condition": "lte", "horizon_days": 3} for r in manager.VOTERS}
    st["portfolio_manager"] = {"direction": "bullish", "confidence": 1.0}
    mk = {"last": 100.0}
    a = manager.aggregate(cfg, st, w, mk)
    # 加权汇总只作参考：方向由 S 定，参考入场=现价；条件单点位以交易经理下单工具为准
    assert a["side"] == "short" and a["condition"] == "now" and a["entry_px"] == pytest.approx(100.0)
    S_with = a["S"]
    st["portfolio_manager"] = {"direction": "bearish", "confidence": 1.0}
    assert manager.aggregate(cfg, st, w, mk)["S"] == S_with          # 组合经理不影响 S
    # 弱共识 → 不交易
    weak = {r: {"direction": "neutral", "confidence": 0.5} for r in manager.VOTERS}
    weak["market"] = {"direction": "bullish", "confidence": 0.5}
    assert manager.aggregate(cfg, weak, w, mk)["side"] is None
    # pm_execute：无持仓 → 挂空单
    coin = cfg["coins"][0]
    from tests._tool_tm import tool_tm
    tm = tool_tm("pending", side="short", trigger_px=98.0, condition="lte", stop_loss=104.0, take_profit=90.0)
    res = manager.pm_execute(cfg, coin, "D-X", st, w, mk, ledger.book(cfg), None, "测试", tm=tm, tm_state="go")
    assert res["action"] == "pending" and pending.load()[coin["inst_id"]]["side"] == "short"
    assert "portfolio_manager" not in pending.load()[coin["inst_id"]]["role_levels"]
