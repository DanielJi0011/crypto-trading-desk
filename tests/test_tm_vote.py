"""交易经理权重/否决/缺席，以及日报限价列与分母说明（全部离线，模拟盘函数全部打桩，不发任何请求）。"""
import json

import pytest

from desk import settings


@pytest.fixture()
def clean():
    from desk import scoring
    for p in (scoring.SCORES, scoring.WEIGHTS, scoring.WEIGHT_HIST):
        if p.exists():
            p.unlink()
    yield scoring
    for p in (scoring.SCORES, scoring.WEIGHTS, scoring.WEIGHT_HIST):
        if p.exists():
            p.unlink()


def _scores(scoring, role, n, hits):
    for i in range(n):
        settings.append_jsonl(scoring.SCORES, {"id": f"{role}{i}", "role": role, "time": f"2026-10-{1 + i % 28:02d} 08:00:{i % 60:02d}",
                                               "hit": i < hits, "regime": "震荡", "direction": "bullish"})


def test_tm_weight_threshold_and_pm_removed(clean):
    scoring = clean
    cfg = settings.load_config()
    cfg["learning"]["apply_weights"] = True   # 本用例验证权重学习本身（生产配置为冻结）
    from desk import manager
    _scores(scoring, "trade_manager", 19, 19)
    scoring.update_weights(cfg)
    w = json.loads(scoring.WEIGHTS.read_text())
    assert w["trade_manager"] == pytest.approx(manager.TM_INIT_WEIGHT) == pytest.approx(1.0)
    assert "portfolio_manager" not in w and "portfolio_manager" not in manager.weights(cfg)
    # L2：权重只看 v2 打分的 skill（旧 v1 记录不计）；同日多币算一个样本
    for i in range(20):
        settings.append_jsonl(scoring.SCORES, {"id": f"v2{i}", "role": "trade_manager", "time": f"2026-11-{1 + i:02d} 08:00:00",
                                               "date": f"2026-11-{1 + i:02d}", "hit": True, "skill": 0.4, "scoring_v": 2,
                                               "regime": "震荡", "direction": "bullish"})
    scoring.update_weights(cfg)
    w = json.loads(scoring.WEIGHTS.read_text())
    assert w["trade_manager"] > 1.0 and manager.weights(cfg)["trade_manager"] == pytest.approx(w["trade_manager"])
    assert "portfolio_manager" not in w


def _dec(**kw):
    from desk.manager import ManagerDecision
    base = dict(action="hold", rationale="t", confidence=0.6)
    base.update(kw)
    return ManagerDecision(**base)


def test_tm_status_rules():
    from desk import manager
    assert manager.tm_status(None) == "absent"
    assert manager.tm_status(_dec()) == "veto"                                   # 中性
    assert manager.tm_status(_dec(action="sell")) == "veto"                      # 无持仓时卖出 = 不入场
    # B1：hold 即使带条件单字段也是中性（今日 BTC 事故形态），冲突被记录
    hold_plan = _dec(entry_side="short", entry_trigger_px=83500.0, entry_condition="gte", entry_stop_loss=85000.0,
                     entry_size_pct=5)
    assert manager.tm_status(hold_plan, mk={"last": 81000}) == "veto"
    assert manager.tm_stance(hold_plan, {"last": 81000})["direction"] == "neutral"
    plan = _dec(action="pending", entry_side="short", entry_trigger_px=2515.0, entry_condition="gte",
                entry_stop_loss=2600.0, entry_size_pct=5, invalidation="站上2600")
    assert manager.tm_status(plan, mk={"last": 2450}) == "go"
    assert manager.tm_stance(plan, {"last": 2450})["direction"] == "bearish"
    bad = _dec(action="pending", entry_side="short", entry_trigger_px=2515.0, entry_condition="gte", entry_size_pct=0)
    assert manager.tm_status(bad) == "veto" and manager.tm_stance(bad, {"last": 2450})["direction"] == "neutral"
    assert manager.tm_status(_dec(action="short", stop_loss=2600), mk={"last": 2450}) == "go"


@pytest.fixture()
def demo(monkeypatch):
    from desk import demo_exec as DX, manager
    calls = {"open_now": 0, "place_trigger": 0, "cancel": []}
    monkeypatch.setattr(DX, "open_now", lambda *a, **k: calls.__setitem__("open_now", calls["open_now"] + 1) or (True, {}))
    monkeypatch.setattr(DX, "place_trigger", lambda *a, **k: calls.__setitem__("place_trigger", calls["place_trigger"] + 1) or (True, {"algoId": "x", "sz": "1"}))
    monkeypatch.setattr(DX, "cancel_triggers", lambda cfg, inst, why: calls["cancel"].append(why) or ["OLD1"])
    cfg = settings.load_config()
    st = {r: {"direction": "bearish", "confidence": 0.8, "stop": 104.0, "target": 90.0, "entry_px": 100.0,
              "entry_condition": "now", "horizon_days": 3} for r in manager.VOTERS if r != "trade_manager"}
    bk = {"equity": 100000.0, "live_equity": 100000.0, "positions": [], "triggers": [], "pending_entries": []}
    monkeypatch.setattr(DX, "book", lambda cfg, prices=None: bk)   # R8：锁内重读账户
    return cfg, manager, calls, st, bk


def test_tm_veto_no_orders(demo):
    cfg, manager, calls, st, bk = demo
    d = _dec()
    st = dict(st, trade_manager=manager.tm_stance(d, {"last": 100.0}))
    res = manager.pm_execute_demo(cfg, cfg["coins"][0], "D-V", st, manager.weights(), {"last": 100.0}, bk, None, "t",
                                  tm=d, tm_state=manager.tm_status(d))
    assert calls["open_now"] == 0 and calls["place_trigger"] == 0
    assert calls["cancel"] == ["交易经理否决"] and "否决" in res["action"]
    assert any("交易经理否决" in n for n in res["notes"])


def test_tm_absent_not_counted_no_new_position(demo, monkeypatch):
    cfg, manager, calls, st, bk = demo
    from desk import llm as dllm

    class Boom:
        def with_structured_output(self, *_):
            return self

        def invoke(self, *_):
            raise RuntimeError("down")
    monkeypatch.setattr(dllm, "make_llm", lambda *a, **k: Boom())
    monkeypatch.setattr(manager, "market_ctx", lambda inst: {"last": 100.0, "ret_20d": 0.0})
    from desk import demo_exec as DX
    monkeypatch.setattr(DX, "is_demo", lambda cfg: True)
    monkeypatch.setattr(DX, "book", lambda cfg, prices=None: bk)
    monkeypatch.setattr(manager, "role_texts", lambda fs: {})
    rec = manager.decide_and_execute(cfg, cfg["coins"][0], "2026-10-21", {}, st, None)
    assert "trade_manager" not in rec["stances"]                 # 这一票不计
    assert rec["consensus"] == pytest.approx(manager.consensus(st, manager.weights()))
    assert rec["tm_status"] == "absent" and "缺席" in rec["final_action"]
    assert calls["open_now"] == 0 and calls["place_trigger"] == 0 and calls["cancel"] == ["tm_absent"]   # R9：缺席也撤未受保护条件单


def test_report_limit_column_and_denominator(monkeypatch):
    from desk import demo_exec as DX, report
    assert report.limit_px({"ordPx": "-1"}) == "市价"
    assert report.limit_px({"ordPx": ""}, {"order_px": "2482.7"}) == "2482.7"
    assert report.limit_px({"orderPx": "108.71"}) == "108.71"
    bk = {"equity": 101898.76, "live_equity": 101700.0, "initial": 101898.76, "cash": 90000.0, "exposure": 4716.28,
          "equity_basis": "initial", "positions": [],
          "triggers": [{"algoId": "A1", "instId": "ETH-USDT-SWAP", "posSide": "short", "side": "sell", "sz": "1", "triggerPx": "2480", "ordPx": "-1"},
                       {"algoId": "A2", "instId": "SOL-USDT-SWAP", "posSide": "short", "side": "sell", "sz": "1", "triggerPx": "108.6", "ordPx": "108.71"}]}
    monkeypatch.setattr(DX, "is_demo", lambda cfg: True)
    monkeypatch.setattr(DX, "book", lambda cfg, prices=None: bk)
    monkeypatch.setattr(DX, "reg", lambda: {"triggers": {}})
    cfg = settings.load_config()
    usage = {"calls": 0, "errors": 0, "input": 0, "cached": 0, "output": 0, "cost_usd": 0, "by_stage": {}, "by_model": {}, "by_provider": {}}
    p = report.write(cfg, "2099-01-01", [], [], [], [], [], usage, {}, [], name="t-report",
                     coin_notes={"ETH-USDT-SWAP": ["09:00 撤单 X，原因：交易经理否决，用户授权"]})
    txt = open(p, encoding="utf-8").read()
    assert "触发后限价" in txt and "| 市价 |" in txt and "| 108.71 |" in txt
    assert "占初始权益 101,898.76 的 4.6%" in txt
    assert "交易经理 |" in txt and "投资组合经理 |" not in txt.split("## 五")[1].split("## 六")[0]
    assert "09:00 撤单 X，原因：交易经理否决，用户授权" in txt


def test_b1_all_roles_neutral_fields_ignored():
    from desk import manager, stances
    s = stances.normalize({"direction": "neutral", "confidence": 0.5, "entry_px": 83500, "entry_condition": "gte", "stop": 85000})
    assert s["entry_px"] is None and s["stop"] is None and "以动作标签为准" in s["field_conflict"]
    assert stances.normalize({"direction": "bearish", "entry_px": 1})["entry_px"] == 1
    cfg = settings.load_config()
    st = {r: {"direction": "neutral", "confidence": 0.9, "entry_px": 83500.0, "entry_condition": "gte", "stop": 85000.0,
              "target": 70000.0} for r in manager.VOTERS}
    agg = manager.aggregate(cfg, st, manager.weights(cfg), {"last": 81000.0, "atr14": 2000.0})
    assert agg["side"] is None and agg["S"] == 0
