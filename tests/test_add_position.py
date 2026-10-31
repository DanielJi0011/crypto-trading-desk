"""同向加仓：加仓腿独立止盈止损，原仓保护不动。预算、削减、不反手、RR、开关、条件单成交不撤旧保护。离线。"""

import pytest

from desk import add_position as AP, demo_exec as DX, okx_demo as X, settings, sizing, equity_guard as EG
from tests.fake_okx import FakeOKX

INSTR = {"ctVal": "0.01", "lotSz": "0.01", "minSz": "0.01", "tickSz": "0.1"}
INST = "BTC-USDT-SWAP"


@pytest.fixture
def base(monkeypatch, tmp_path):
    monkeypatch.setattr(X, "_inst_cache", {INST: INSTR, "ETH-USDT-SWAP": INSTR})
    monkeypatch.setattr(X, "EVENTS", tmp_path / "ev.jsonl")
    monkeypatch.setattr(EG, "MARKS", tmp_path / "m.json")
    monkeypatch.setattr(DX, "REG", tmp_path / "reg.json")
    monkeypatch.setattr(DX, "today_realized", lambda: 0.0); monkeypatch.setattr(DX, "today_orders", lambda: 0)
    return settings.load_config()


def _short(qty=0.0604, avg=80760.4, sl=82600.0, tp=77000.0, lev=3.0, px=80000.0):
    return {"inst_id": INST, "side": "short", "pos_side": "short", "qty": qty, "contracts": round(qty / 0.01, 6), "avg_px": avg,
            "price": px, "sl_px": sl, "tp_px": tp, "leverage": lev, "margin": qty * px / lev, "notional": qty * px, "upnl": 0.0}


def _bk(positions, eq=100000.0):
    return {"equity": eq, "live_equity": eq, "positions": positions, "triggers": [], "pending_entries": [],
            "exposure": sum(p["notional"] for p in positions), "cash": eq}


def _rc(cfg):
    return sizing.rcfg(cfg)


# ---------------------------------------------------------------- 预算 / 独立止盈止损
def test_budget_success(base):
    p = _short()
    add_sl = 82000.0
    a = AP.budget(_rc(base), p, 80000, add_sl, {"risk_usd": 750.0, "notional": 10000.0}, 100000, 20, 5)
    assert a["ok"] and not a["clipped"] and a["notional"] == pytest.approx(10000)
    assert a["existing_risk"] == pytest.approx(0.0604 * (82600 - 80760.4))
    assert a["existing_risk"] + a["add_risk"] <= a["cap"] + 1e-6


def test_budget_full(base):
    p = _short(qty=0.5)
    a = AP.budget(_rc(base), p, 80000, 82000, {"risk_usd": 750.0, "notional": 10000.0}, 100000, 20, 5)
    assert not a["ok"] and a["reason"] == "add_budget_full" and a["notional"] == 0


def test_budget_partial_clip(base):
    p = _short(qty=0.3)
    a = AP.budget(_rc(base), p, 80000, 82000, {"risk_usd": 750.0, "notional": 50000.0}, 100000, 20, 5)
    assert a["ok"] and a["clipped"] and "add_risk_budget" in a["clip_by"]
    assert a["existing_risk"] + a["add_risk"] == pytest.approx(a["cap"], rel=1e-6)


def test_existing_risk_zero_when_stop_in_profit(base):
    assert AP.existing_risk(_short(sl=80000), _rc(base)) == 0.0


def test_plan_sl_tp_independent_rr(base):
    rc = _rc(base)
    r = AP.plan_sl_tp(rc, "short", 80000, 2500, role_stop=None, role_tp=79500)
    assert r["stop"] is not None and r["rr"] >= 1.5 - 1e-9
    assert r["stop"] > 80000 and r["take_profit"] < 80000
    # 与原仓 82600 无关：本腿止损由 ATR 决定，不一定等于原仓
    assert abs(r["stop"] - 82600) > 1


def test_describe_separates_legs(base):
    s = AP.describe({"existing_risk": 100, "add_risk": 50, "cap": 750, "budget_left": 600, "clipped": False,
                     "base_sl": 82600, "base_tp": 77000, "add_px": 80000, "final_sl": 82000, "final_tp": 76000,
                     "rr": 1.5, "sz": "3"})
    assert "原仓止损 82600" in s and "加仓腿入场 80000" in s and "止损 82000" in s


# ---------------------------------------------------------------- check_open
def test_check_open_add_mode(base):
    p = _short()
    bk = _bk([p])
    r0, _ = DX.check_open(base, bk, INST, 0.05, 80000, 82000, "short", 3)
    assert any("[ONE_PER_COIN]" in x for x in r0)
    r1, ch = DX.check_open(base, bk, INST, 0.05, 80000, 82000, "short", 3, add=p)
    assert not any("[ONE_PER_COIN]" in x for x in r1), r1
    assert ch["existing_risk_usd"] > 0
    r2, _ = DX.check_open(base, bk, INST, 0.05, 80000, 78000, "long", 3, add=p)
    assert any("[ADD]" in x for x in r2)


# ---------------------------------------------------------------- 组合经理
# ---------------------------------------------------------------- 独立保护单 / 不撤旧
def test_place_leg_protection_keeps_old(base, monkeypatch):
    f = FakeOKX().install(monkeypatch)
    f.algos = [{"instId": INST, "ordType": "oco", "algoId": "OLD", "posSide": "short", "sz": "6.04",
                "slTriggerPx": "82600", "tpTriggerPx": "77000", "reduceOnly": True,
                "side": "buy"}]
    nid = DX.place_leg_protection(_short(), 82000, 76000, 3.0)
    ids = {a["algoId"] for a in f.algos}
    assert nid and "OLD" in ids and nid in ids
    add = next(a for a in f.algos if a["algoId"] == nid)
    assert add["sz"] == "3" and float(add["slTriggerPx"]) == 82000


def test_add_now_keeps_old_protect_and_registers_leg(base, monkeypatch):
    f = FakeOKX().install(monkeypatch)
    f.algos = [{"instId": INST, "ordType": "oco", "algoId": "OLD", "posSide": "short", "sz": "6.04",
                "slTriggerPx": "82600", "side": "buy", "reduceOnly": True}]
    def post(path, body):
        f.calls.append(("POST", path, body))
        if path.endswith("order"):
            # 模拟附带止损：为加仓张数挂独立 algo
            aid = "ADD_ATT"
            f.algos.append({"instId": INST, "ordType": "oco", "algoId": aid, "posSide": "short",
                            "sz": body["sz"], "slTriggerPx": body["attachAlgoOrds"][0]["slTriggerPx"],
                            "tpTriggerPx": body["attachAlgoOrds"][0].get("tpTriggerPx"), "side": "buy", "reduceOnly": True})
            return {"data": [{"ordId": "O1", "sCode": "0"}]}
        if path.endswith("order-algo"):
            aid = str(next(f._id)); f.algos.append({**body, "algoId": aid}); return {"data": [{"algoId": aid}]}
        if path.endswith("cancel-algos"):
            ids = {x["algoId"] for x in body}; f.algos = [a for a in f.algos if a["algoId"] not in ids]
            return {"data": [{}]}
        return {"data": [{}]}
    monkeypatch.setattr(X, "post", post)
    monkeypatch.setattr(DX, "check_open", lambda *a, **k: ([], {}))
    monkeypatch.setattr(DX, "trigger_orders", lambda inst=None: [])
    monkeypatch.setattr(DX, "pending_entry_orders", lambda inst=None: [])
    p = _short()
    ok, info = DX.add_now(base, _bk([p]), INST, p, "3", 82000, 76000, "D1", 80000,
                          {"existing_risk": 111, "add_risk": 60, "budget_left": 500, "clipped": False, "rr": 1.5})
    assert ok and "OLD" in info["old_protect_kept"]
    assert any(a["algoId"] == "OLD" for a in f.algos)
    assert not any(c[0] == "POST" and c[1].endswith("cancel-algos") for c in f.calls)
    r = DX.reg()
    legs = r["positions"][INST]["legs"]
    assert any(lg["kind"] == "open" and lg["sl"] == 82600 for lg in legs)
    assert any(lg["kind"] == "add" and lg["sl"] == 82000 and lg["sz"] == "3" for lg in legs)


def test_recheck_does_not_cancel_old_protect(base, monkeypatch):
    f = FakeOKX().install(monkeypatch)
    f.algos = [{"instId": INST, "ordType": "oco", "algoId": "OLD", "posSide": "short", "sz": "6.04",
                "slTriggerPx": "82600", "side": "buy", "reduceOnly": True}]
    after = _short(qty=0.0904, avg=80500, sl=82600)  # +3 张
    monkeypatch.setattr(DX, "book", lambda cfg, prices=None: _bk([after]))
    monkeypatch.setattr(DX, "_reduce", lambda *a, **k: pytest.fail("不应减仓"))
    res = AP.recheck(base, INST, {"base_contracts": 6.04, "base_avg": 80760.4, "base_sl": 82600, "base_tp": 77000,
                                  "base_qty": 0.0604, "add_px": 80000, "risk_cap_usd": 750.0,
                                  "final_sl": 82000, "final_tp": 76000, "algoId": "T1"})
    assert res["result"] == "ok" and "OLD" in res["old_protect_kept"]
    assert res["protect_algoId"] and res["protect_algoId"] != "OLD"
    assert any(a["algoId"] == "OLD" for a in f.algos)
    add = next(a for a in f.algos if a["algoId"] == res["protect_algoId"])
    assert abs(float(add["sz"]) - 3.0) < 1e-9


def test_recheck_reduce_keeps_old(base, monkeypatch):
    f = FakeOKX().install(monkeypatch)
    f.algos = [{"instId": INST, "ordType": "oco", "algoId": "OLD", "posSide": "short", "sz": "6.04",
                "slTriggerPx": "82600", "side": "buy", "reduceOnly": True}]
    after = _short(qty=0.5, avg=80500, sl=82600)
    monkeypatch.setattr(DX, "book", lambda cfg, prices=None: _bk([after]))
    red = []
    def _red(cfg, p, frac, why, restitch_protection=True):
        red.append({"frac": frac, "restitch": restitch_protection})
        return {"ok": True}
    monkeypatch.setattr(DX, "_reduce", _red)
    # 减仓后账面仍用 after，简化：place_leg 仍会挂
    res = AP.recheck(base, INST, {"base_contracts": 6.04, "base_avg": 80760.4, "base_sl": 82600,
                                  "base_qty": 0.0604, "add_px": 80000, "risk_cap_usd": 200.0,
                                  "final_sl": 82000, "final_tp": 76000, "algoId": "T1"})
    assert res["result"].startswith("reduced") and red and red[0]["restitch"] is False
    assert "OLD" in res["old_protect_kept"]


def test_on_fill_waits_then_rechecks(base, monkeypatch):
    from desk import entry_limit as EL
    state = {"s": "live"}
    monkeypatch.setattr(X, "get", lambda path, **kw: {"data": [{"state": state["s"]}]})
    monkeypatch.setattr(EL, "_load", lambda: {"entries": {}})
    done = []
    monkeypatch.setattr(AP, "recheck", lambda cfg, inst, m: done.append(m) or {"result": "ok"})
    r = {"add_fills": {"T1": {"inst_id": INST, "child_ordId": "C1", "base_contracts": 6.04}}}
    assert AP.on_fill(base, r) == [("T1", "waiting")] and "T1" in r["add_fills"]
    state["s"] = "filled"
    assert AP.on_fill(base, r) == [("T1", "ok")] and not r["add_fills"] and done


def test_entry_limit_add_protect_keeps_old(base, monkeypatch):
    from desk import entry_limit as EL
    f = FakeOKX().install(monkeypatch)
    f.algos = [{"instId": INST, "ordType": "oco", "algoId": "OLD", "posSide": "short", "sz": "6.04",
                "slTriggerPx": "82600", "side": "buy", "reduceOnly": True}]
    e = {"inst": INST, "side": "short", "pos_side": "short", "stop_loss": 82000, "take_profit": 76000,
         "accFillSz": "3", "add": {"base_contracts": 6.04}}
    nid = EL._protect_total("T1", e)
    assert nid and any(a["algoId"] == "OLD" for a in f.algos)
    assert not any(c[0] == "POST" and c[1].endswith("cancel-algos") for c in f.calls)


def test_switch_default_true(base):
    assert AP.enabled({}) is True and AP.enabled({"risk": {"allow_add_position": False}}) is False


def test_no_consolidate_protection_symbol(base):
    assert not hasattr(DX, "consolidate_protection")
