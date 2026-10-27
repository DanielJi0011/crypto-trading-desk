"""交易经理决策前的加仓预览：独立腿止损止盈/RR/张数/预算；只读；算不出写原因；提示词与日报。离线。"""
import types

import pytest

from desk import add_position as AP, demo_exec as DX, manager as MG, okx_demo as X, settings, equity_guard as EG

INSTR = {"ctVal": "0.1", "lotSz": "0.01", "minSz": "0.01", "tickSz": "0.01"}
INST = "ETH-USDT-SWAP"


@pytest.fixture
def base(monkeypatch, tmp_path):
    monkeypatch.setattr(X, "_inst_cache", {INST: INSTR})
    monkeypatch.setattr(X, "EVENTS", tmp_path / "ev.jsonl")
    monkeypatch.setattr(EG, "MARKS", tmp_path / "m.json")
    monkeypatch.setattr(DX, "REG", tmp_path / "reg.json")
    monkeypatch.setattr(MG, "demo_size", lambda *a: {"ok": True, "risk_usd": 750.0, "notional": 20000.0, "clipped": []})
    return settings.load_config()


def _pos(sl=2445.0, avg=2350.0, qty=1.0):
    return {"inst_id": INST, "side": "short", "pos_side": "short", "qty": qty, "contracts": qty / 0.1, "avg_px": avg,
            "price": 2380.0, "sl_px": sl, "tp_px": 2200.0, "leverage": 3.0, "margin": qty * 2380 / 3, "notional": qty * 2380,
            "upnl": 0.0, "liq_px": 3000, "deadline": "-"}


def _bk(p, eq=100000.0):
    return {"equity": eq, "live_equity": eq, "positions": [p], "triggers": [], "pending_entries": [], "exposure": p["notional"], "cash": eq}


def _st():
    s = {"direction": "bearish", "confidence": 0.7, "entry_px": 2400.0, "stop": None, "target": None, "horizon_days": 5, "reason": "x"}
    return {"trader": dict(s), "portfolio_manager": {**s, "entry_px": None}}


MK = {"last": 2380.0, "atr14": 90.0, "bar_date": "2026-10-27", "close": 2380.0, "ret_5d": 0, "ret_20d": 0, "hi_20": 2500, "lo_20": 2200}


def test_preview_independent_leg_not_base_stop(base):
    p = _pos()
    pv = MG.add_preview(base, INST, MK, _bk(p), p, _st(), {"trader": 1.0, "portfolio_manager": 1.0}, -0.5)
    it = next(i for i in pv["items"] if i["add_px"] == 2400.0)
    assert "交易员" in "/".join(it["by"]) or it["by"]
    assert it["stop"] > 2400 and it["stop"] != 2445.0 and it["tp"] < 2400
    assert it["rr"] >= 1.5 - 1e-9 and it["sz"] and it["within_budget"] is True
    assert it["existing_risk"] + it["add_risk"] <= it["cap"] + 1e-6
    assert any(i["by"] == ["现价"] for i in pv["items"])
    txt = MG.describe_add_preview(pv)
    assert "原仓止损 2445" in txt and "不动" in txt and "本腿止损" in txt


def test_preview_readonly_no_orders(base, monkeypatch):
    for n in ("add_now", "place_add_trigger", "place_leg_protection", "save_reg"):
        monkeypatch.setattr(DX, n, lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应下单/写登记")))
    monkeypatch.setattr(X, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应调用交易接口")))
    p = _pos(); before = dict(p)
    MG.add_preview(base, INST, MK, _bk(p), p, _st(), {"trader": 1.0}, -0.5)
    assert p == before


def test_preview_budget_full_reason(base):
    p = _pos(qty=50.0)   # 原仓风险远超 0.75%
    pv = MG.add_preview(base, INST, MK, _bk(p), p, _st(), {"trader": 1.0}, -0.5)
    assert all(i["within_budget"] is False and i["reason"] == "add_budget_full" for i in pv["items"])
    assert "算不出/不可加：add_budget_full" in MG.describe_add_preview(pv)


def test_preview_failure_does_not_raise(base, monkeypatch):
    monkeypatch.setattr(MG, "demo_size", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    p = _pos()
    pv = MG.add_preview(base, INST, MK, _bk(p), p, _st(), {"trader": 1.0}, -0.5)
    assert pv["items"] and all("预览计算失败" in i["reason"] for i in pv["items"])


def test_preview_none_when_flat_or_switch_off(base):
    p = _pos()
    assert MG.add_preview(base, INST, MK, _bk(p), None, _st(), {}, -0.5) is None
    off = {**base, "risk": {**(base.get("risk") or {}), "allow_add_position": False}}
    assert MG.add_preview(off, INST, MK, _bk(p), p, _st(), {}, -0.5) is None


def test_preview_paper_account_reason(base):
    p = _pos(); bk = _bk(p); bk.pop("live_equity")
    pv = MG.add_preview(base, INST, MK, bk, p, _st(), {"trader": 1.0}, -0.5)
    assert all("非模拟盘" in i["reason"] for i in pv["items"]) and all(i["stop"] for i in pv["items"])


def test_prompt_contains_preview_and_rule(base):
    p = _pos(); bk = _bk(p)
    from desk.roles import ROLE_KEYS
    st = {r: {"direction": "neutral", "confidence": 0.5} for r in ROLE_KEYS}; st.update(_st())
    pv = MG.add_preview(base, INST, MK, bk, p, st, {"trader": 1.0}, -0.5)
    txt = MG._prompt(base, {"name": "ETH", "inst_id": INST}, MK, p, bk, st, {"trader": 1.0}, -0.5,
                     {"portfolio_manager": "pm", "trader": "tr"}, pv)
    assert "## 加仓预览" in txt and "不要拿原仓止损止盈" in txt and "否决权不变" in txt and "本腿止损" in txt


def test_report_shows_preview():
    pv = {"base_sl": 2445.0, "base_tp": 2200.0, "error": None,
          "items": [{"by": ["交易员"], "add_px": 2400.0, "stop": 2535.0, "tp": 2197.5, "rr": 1.5, "sz": "3",
                     "existing_risk": 95.0, "add_risk": 400.0, "cap": 750.0, "within_budget": True, "reason": None}]}
    s = MG.describe_add_preview(pv)
    assert "交易员 入场 2400" in s and "本腿止损 2535" in s and "0.75%预算内" in s
