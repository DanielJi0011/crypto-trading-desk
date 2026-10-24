"""交易经理 new_tp 执行与共识口径标注（离线，不调 AI、不发网络）。"""
import contextlib
import pytest
from types import SimpleNamespace as NS

from desk import okx_demo as X, demo_exec as DX, manager as M, okx_data, exec_lock, settings

INST = "BTC-USDT-SWAP"


@pytest.fixture
def env(monkeypatch, tmp_path):
    calls = {"post": [], "replace": []}
    algos = [{"instId": INST, "algoId": "A1", "posSide": "short", "sz": "9.86", "slTriggerPx": "82250", "tpTriggerPx": "79750"}]
    monkeypatch.setattr(DX, "_require", lambda: None)
    monkeypatch.setattr(DX, "close_algos", lambda inst=None: algos)
    monkeypatch.setattr(exec_lock, "hold", lambda inst, why="": contextlib.nullcontext())
    monkeypatch.setattr(okx_data, "last_price", lambda inst: 81000.0)
    monkeypatch.setattr(X, "post", lambda p, b: calls["post"].append((p, b)) or [{}])
    monkeypatch.setattr(X, "log_event", lambda *a, **k: None)
    monkeypatch.setattr(X, "_px", lambda inst, px: str(px))
    monkeypatch.setattr(DX, "_replace_close_algos", lambda p, sl, tp, sz: calls["replace"].append((sl, tp, sz)) or "A2")
    calls["algos"] = algos
    return calls


CFG = {"manager": {"tp_min_gap_pct": 0.3}}
POS = {"inst_id": INST, "side": "short", "pos_side": "short", "contracts": 9.86, "venue": "okx_demo", "price": 81000.0}


def test_amend_same_oco_farther_and_closer(env):
    ok, _ = DX.move_tp(CFG, POS, 78000)
    assert ok and env["post"][0][0] == "/api/v5/trade/amend-algos"
    assert env["post"][0][1]["algoId"] == "A1" and env["post"][0][1]["newTpTriggerPx"] == "78000"
    ok, _ = DX.move_tp(CFG, POS, 80500)
    assert ok and len(env["post"]) == 2


@pytest.mark.parametrize("tp", [82000, 81000, 80900])
def test_reject_wrong_side_or_immediate(env, tp):
    ok, msg = DX.move_tp(CFG, POS, tp)
    assert not ok and "盈利一侧" in msg and not env["post"]


def test_long_side(env):
    env["algos"][0]["posSide"] = "long"
    p = {**POS, "side": "long", "pos_side": "long"}
    assert not DX.move_tp(CFG, p, 80000)[0]
    assert DX.move_tp(CFG, p, 83000)[0]


def test_size_mismatch_replaces_with_remaining(env):
    ok, _ = DX.move_tp(CFG, {**POS, "contracts": 5}, 78000)
    assert ok and env["replace"] == [(82250.0, 78000, X._dec(5))] and not env["post"]


def test_no_stop_order_refuses(env):
    env["algos"].clear()
    ok, msg = DX.move_tp(CFG, POS, 78000)
    assert not ok and "没有止损单" in msg


def test_apply_new_tp_paths(monkeypatch):
    seen = []
    monkeypatch.setattr(DX, "move_tp", lambda cfg, p, tp: seen.append((p["contracts"], tp)) or (False, "拒绝原因"))
    d = NS(new_tp=78000.0)
    n = []; M.apply_new_tp(CFG, POS, d, "hold", None, n)
    assert seen == [(9.86, 78000.0)] and n == ["拒绝原因"]
    n = []; M.apply_new_tp(CFG, POS, d, "reduce", {"ok": True, "remaining": 5}, n)
    assert seen[-1] == (5, 78000.0)
    n = []; M.apply_new_tp(CFG, POS, d, "reduce", {"ok": False}, n)
    assert "减仓未成功" in n[0] and len(seen) == 2
    n = []; M.apply_new_tp(CFG, {**POS, "venue": "paper"}, d, "hold", None, n)
    assert "非模拟盘" in n[0]
    n = []; M.apply_new_tp(CFG, POS, d, "close", {"ok": True}, n)
    assert n == [] and len(seen) == 2
    monkeypatch.setattr(DX, "move_tp", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    n = []; M.apply_new_tp(CFG, POS, d, "hold", None, n)
    assert "失败" in n[0]


def test_decision_has_new_tp():
    assert "new_tp" in M.ManagerDecision.model_fields


def test_prompt_labels_consensus_basis():
    import inspect
    src = inspect.getsource(M)
    assert "不含你本人投票" in src and "consensus_pre_tm" in src
    from pathlib import Path
    assert "consensus_pre_tm" in (Path(__file__).resolve().parents[1] / "routine_prompt.md").read_text()
