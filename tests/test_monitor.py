"""离线测试：条件入场单触发/过期/护栏拒单/confirm 路径（模拟 LLM）、多空永续、强平、聚合、止损后再讨论限频。"""
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
    monkeypatch.setattr(monitor, "LAST", lambda inst: 100.0)
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


def test_idle_no_llm_no_market(env, monkeypatch):
    cfg, ledger, monitor, pending, _ = env
    monkeypatch.setattr(monitor, "MIN_BARS", lambda *a: (_ for _ in ()).throw(AssertionError("不应请求行情")))
    monkeypatch.setattr(monitor, "CONFIRM", lambda *a: (_ for _ in ()).throw(AssertionError("不应调用 LLM")))
    assert monitor.run_once(cfg) == {"idle": True}


def test_long_breakout_fill_with_gap(env, monkeypatch):
    cfg, ledger, monitor, pending, _ = env
    o = pending.place(cfg, "BTC-USDT-SWAP", order())
    t0 = o["created_ts_ms"] + 60_000
    # 第 2 根跳空高开 107 > 触发 105 → 按开盘价 107 成交（再加滑点）
    monkeypatch.setattr(monitor, "MIN_BARS", lambda inst, bar: bars([(100, 104, 99, 103), (107, 108, 106, 107)], t0))
    res = monitor.process_pending(cfg)
    assert res[0]["result"] == "成交" and res[0]["px"] == pytest.approx(107 * 1.0005)
    t = ledger.open_trades()[0]
    m = ledger.meta()[t["trade_id"]]
    # 1x：保证金 = 名义 / 1；名义 = size_pct 10% × 权益 1000 × 1 = 100（qty 向下取整到 1e-8）
    fill = 107 * 1.0005
    assert m["side"] == "long" and m["leverage"] == 1 and m["margin_usd"] == pytest.approx(float(t["notional_usd"]))
    assert float(t["notional_usd"]) == pytest.approx(100.0, abs=1e-5)
    # liq_price(long) = entry × (1 − 1/lev + mmr) = entry × (1 − 1 + 0.005)
    assert m["liq_px"] == pytest.approx(fill * 0.005)
    assert pending.load() == {}
    assert [e["type"] for e in pending.events()][-2:] == ["触发", "成交"]


def test_short_pullback_fill_and_stop_on_high(env, monkeypatch):
    cfg, ledger, monitor, pending, _ = env
    o = pending.place(cfg, "ETH-USDT-SWAP", order(side="short", trigger_px=95.0, condition="lte", stop_loss=100.0,
                                                   take_profit=85.0, consensus=-0.5))
    t0 = o["created_ts_ms"] + 60_000
    monkeypatch.setattr(monitor, "MIN_BARS", lambda inst, bar: bars([(97, 98, 94.5, 95)], t0))
    res = monitor.process_pending(cfg)
    assert res[0]["result"] == "成交" and res[0]["side"] == "short" and res[0]["px"] == pytest.approx(95 * 0.9995)
    # 空单：High 触止损（同一根 K 线也触及止盈 → 按止损）
    monkeypatch.setattr(ledger, "BARS", lambda inst: bars([(96, 101, 84, 90)], t0 + 3600_000))
    c = ledger.mark_to_market(cfg)
    assert c[0]["exit_reason"] == "止损" and float(c[0]["exit_px"]) == 100 and float(c[0]["pnl_usd"]) < 0


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


def test_expiry(env, monkeypatch):
    cfg, ledger, monitor, pending, _ = env
    pending.place(cfg, "BTC-USDT-SWAP", order(expire_hours=1))
    res = monitor.process_pending(cfg, now=settings.now() + dt.timedelta(hours=2))
    assert res[0]["result"] == "过期" and pending.load() == {}


def test_replace_one_per_coin(env):
    cfg, ledger, monitor, pending, _ = env
    pending.place(cfg, "BTC-USDT-SWAP", order())
    pending.place(cfg, "BTC-USDT-SWAP", order(trigger_px=110.0))
    assert len(pending.load()) == 1 and pending.load()["BTC-USDT-SWAP"]["trigger_px"] == 110.0
    assert "替换" in [e["type"] for e in pending.events()]


@pytest.mark.parametrize("case", ["kill", "existing", "consensus", "stop", "daily_loss"])
def test_guardrail_rejections(env, monkeypatch, case):
    cfg, ledger, monitor, pending, _ = env
    kw = {}
    if case == "kill":
        ledger.kill_switch_path(cfg).write_text("x")
    if case == "existing":
        assert ledger.open_position(cfg, "BTC-USDT-SWAP", 100, 95.0, None, 5, "D0", "t", 1000)[0]
    if case == "consensus":
        kw["consensus"] = 0.05
    if case == "stop":
        kw["stop_loss"] = 110.0
    if case == "daily_loss":
        # 1x：名义 200（保证金 200 = 单笔上限 20%）；入场 100.05，在 70 平仓
        # 亏 ≈ 1.999×(70−100.05) − 费 ≈ −60.2，超过权益 5%（≈47）→ DAILY_LOSS
        assert ledger.open_position(cfg, "ETH-USDT-SWAP", 200, 90.0, None, 5, "D0", "t", 1000, leverage=1)[0]
        ledger.close_position(cfg, ledger.open_trades()[0]["trade_id"], "测试", px=70.0)
    o = pending.place(cfg, "BTC-USDT-SWAP", order(**kw))
    monkeypatch.setattr(monitor, "MIN_BARS", lambda inst, bar: bars([(104, 106, 103, 105)], o["created_ts_ms"] + 60_000))
    res = monitor.process_pending(cfg)
    assert res[0]["result"] == "拒单", res
    assert pending.load() == {}
    if case == "daily_loss":
        assert any("DAILY_LOSS" in r for r in res[0]["reasons"])
    if case == "kill":
        ledger.kill_switch_path(cfg).unlink()


def test_confirm_path(env, monkeypatch):
    cfg, ledger, monitor, pending, _ = env
    cfg = {**cfg, "pending": {**cfg["pending"], "mode": "confirm", "near_pct": 0.5}}
    seen = []

    def fake_confirm(c, coin, o, ctx):
        seen.append(ctx)
        return monitor.EntryConfirm(confirm=len(seen) > 1, reason="模拟")
    monkeypatch.setattr(monitor, "CONFIRM", fake_confirm)
    # 第一次：价格进入 0.5% 以内（104.6）但未触及 → 请求确认 → 跳过 → 撤单
    o = pending.place(cfg, "BTC-USDT-SWAP", order())
    monkeypatch.setattr(monitor, "MIN_BARS", lambda inst, bar: bars([(104, 104.6, 103, 104.5)], o["created_ts_ms"] + 60_000))
    assert monitor.process_pending(cfg)[0]["result"] == "跳过" and not seen[0]["hit"]
    assert pending.load() == {}
    # 第二次：真实触及 → 确认 → 成交
    o = pending.place(cfg, "BTC-USDT-SWAP", order())
    monkeypatch.setattr(monitor, "MIN_BARS", lambda inst, bar: bars([(104, 106, 103, 105.5)], o["created_ts_ms"] + 60_000))
    assert monitor.process_pending(cfg)[0]["result"] == "成交" and seen[1]["hit"]


def test_stop_triggers_rediscuss_once_with_cooldown(env, monkeypatch):
    cfg, ledger, monitor, pending, calls = env
    ok, info = ledger.open_position(cfg, "BTC-USDT-SWAP", 200, 95.0, None, 5, "D", "t", 1000)
    t0 = int(settings.now().timestamp() * 1000) + 60_000
    monkeypatch.setattr(monitor, "MIN_BARS", lambda inst, bar: bars([(96, 97, 94, 95)], t0))
    monitor.run_once(cfg)
    assert calls == [["--coin", "BTC", "--event", info["trade_id"], "--reason", "sl"]]
    tr = [t for t in ledger.store.read_trades() if t["trade_id"] == info["trade_id"]][0]
    assert monitor.schedule_rediscuss(cfg, tr) == "该事件已讨论过"
    ok, info2 = ledger.open_position(cfg, "BTC-USDT-SWAP", 200, 95.0, None, 5, "D2", "t", 1000)
    tr2 = dict(tr, trade_id=info2["trade_id"])
    assert monitor.schedule_rediscuss(cfg, tr2) == "冷却中" and len(calls) == 1


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
    assert a["side"] == "short" and a["condition"] == "lte" and a["entry_px"] == pytest.approx(98.0)
    assert a["stop_loss"] == pytest.approx(104.0) and a["take_profit"] == pytest.approx(90.0)
    S_with = a["S"]
    st["portfolio_manager"] = {"direction": "bearish", "confidence": 1.0}
    assert manager.aggregate(cfg, st, w, mk)["S"] == S_with          # 组合经理不影响 S
    # 弱共识 → 不交易
    weak = {r: {"direction": "neutral", "confidence": 0.5} for r in manager.VOTERS}
    weak["market"] = {"direction": "bullish", "confidence": 0.5}
    assert manager.aggregate(cfg, weak, w, mk)["side"] is None
    # pm_execute：无持仓 → 挂空单
    coin = cfg["coins"][0]
    res = manager.pm_execute(cfg, coin, "D-X", st, w, mk, ledger.book(cfg), None, "测试")
    assert res["action"] == "pending" and pending.load()[coin["inst_id"]]["side"] == "short"
    assert "portfolio_manager" not in pending.load()[coin["inst_id"]]["role_levels"]
