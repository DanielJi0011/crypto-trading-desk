"""2026-10-31 用户决定：交易经理必须给具体点位（缺项视为无结论）；同价位保留挂单时止损以交易经理为准（只过硬护栏）；
挂单提醒来源标签按决策号场次。离线。"""
import pytest

from desk import demo_exec as DX, demo_ws, manager, settings, sizing
from tests.test_pending_entry_fix import _run, EXT, _tm as _ptm
from tests.test_tm_no_vote import _dec, _st, desk_env  # noqa: F401


@pytest.fixture
def cfg():
    return settings.load_config()


def _agg(cfg, tm):
    return manager.aggregate(cfg, _st("bearish"), manager.weights(cfg), {"last": 100.0, "atr14": 2.0}, tm=tm)


# ---------- 1. 具体点位必填 ----------
def test_config_defaults_true(cfg):
    assert cfg["manager"]["tm_require_explicit_levels"] is True and cfg["manager"]["tm_pending_stop_by_tm"] is True


@pytest.mark.parametrize("kw,miss", [
    (dict(action="short", stop_loss=104.0, take_profit=92.0), "入场价"),
    (dict(action="short", entry=100.0, take_profit=92.0), "止损"),
    (dict(action="short", entry=100.0, stop_loss=104.0), "止盈"),
    (dict(action="pending", entry_side="short", entry_trigger_px=103.0, entry_condition="gte", entry_stop_loss=106.0,
          entry_size_pct=5), "止盈"),
])
def test_missing_level_no_conclusion(cfg, kw, miss):
    agg = _agg(cfg, _dec(**kw))
    assert agg["buy"] is False and agg["tm_no_conclusion"] and miss in agg["tm_missing_levels"]
    assert any("未给具体" in n for n in agg["notes"])


def test_full_levels_still_open(cfg):
    agg = _agg(cfg, _dec(action="short", entry=100.0, stop_loss=104.0, take_profit=92.0))
    assert agg["buy"] and agg["plan_group"] == "tm" and 100.0 < agg["stop_loss"] <= 104.0
    agg = _agg(cfg, _dec(action="pending", entry_side="short", entry_trigger_px=103.0, entry_condition="gte",
                         entry_stop_loss=106.0, entry_take_profit=95.0, entry_size_pct=5))
    assert agg["buy"] and agg["plan_group"] == "tm_pending" and not agg["tm_missing_levels"]


def test_switch_off_old_behavior(cfg):
    cfg["manager"]["tm_require_explicit_levels"] = False
    agg = _agg(cfg, _dec(action="short", stop_loss=104.0, take_profit=None))
    assert agg["buy"] and agg["plan_group"] == "tm" and not agg["tm_missing_levels"]


def test_hold_sell_reduce_not_affected():
    for a in ("hold", "sell", "reduce"):
        assert manager.tm_missing_levels(_dec(action=a)) == []


def test_decide_missing_levels_notifies(desk_env, monkeypatch):
    m, calls, box = desk_env
    sent = []
    monkeypatch.setattr(demo_ws, "notify_tm_no_conclusion", lambda inst, why, did, src: sent.append((inst, why, did)) or True)
    cfg = settings.load_config()
    box["dec"] = _dec(action="short", size_pct=5, entry=100.0, stop_loss=104.0)   # 缺止盈
    rec = m.decide_and_execute(cfg, cfg["coins"][0], "2026-10-30", {}, _st("bearish"), None)
    assert not calls["open_now"] and not calls["place_trigger"]
    assert sent and "止盈" in sent[0][1] and rec["notes"][0].startswith(manager.TM_NO_CONCLUSION_NOTE)


def test_prompt_requires_concrete_levels():
    src = open(manager.__file__, encoding="utf-8").read()
    assert "点位必须具体（硬性要求）" in src and "不得只写区间" in src and "止损止盈不动" in src


# ---------- 2. 保留挂单止损以交易经理为准 ----------
F = sizing.pending_keep_sltp


def test_pending_widen_default_rejected():
    r = F("short", 2334, 2432, 2185, 2480, 2100, 2290)
    assert r["stop_loss"] == 2432 and any("只允许收紧" in x for x in r["reasons"])


def test_pending_widen_allowed_with_guards():
    r = F("short", 2334, 2432, 2185, 2480, 2100, 2290, allow_widen=True, stop_max_pct=0.08, widen_check=lambda s: None)
    assert r["stop_loss"] == 2480 and r["take_profit"] == 2100 and r["changed"]
    r = F("short", 2334, 2432, 2185, 2530, 2100, 2290, allow_widen=True, stop_max_pct=0.08)   # >8%
    assert r["stop_loss"] == 2432 and any("超过上限" in x for x in r["reasons"])
    r = F("short", 2334, 2432, 2185, 2480, 2100, 2290, allow_widen=True, stop_max_pct=0.08, widen_check=lambda s: "单笔风险超限")
    assert r["stop_loss"] == 2432 and any("单笔风险超限" in x for x in r["reasons"])
    r = F("short", 2334, 2432, 2185, 2480, 2160, 2290, allow_widen=True, stop_max_pct=0.08)     # RR 1.19<1.5
    assert (r["stop_loss"], r["take_profit"]) == (2432, 2185) and any("RR" in x for x in r["reasons"])
    r = F("short", 2334, 2432, 2185, 2300, 2100, 2290, allow_widen=True, stop_max_pct=0.08)     # 错误一侧
    assert r["stop_loss"] == 2432


def test_keep_same_trigger_widen_by_tm(monkeypatch):
    from desk import okx_demo
    monkeypatch.setattr(okx_demo, "instrument", lambda inst: {"ctVal": "0.1"})
    r, c = _run(monkeypatch, dict(EXT), _ptm(entry_stop_loss=2480, entry_take_profit=2100))
    assert r["action"] == "pending" and c["trig"] == [] and c["amend"] == [(2480.0, 2100.0)]


def test_keep_same_trigger_widen_risk_guard(monkeypatch):
    from desk import okx_demo
    monkeypatch.setattr(okx_demo, "instrument", lambda inst: {"ctVal": "10"})   # 3 张×10×146 > 1.5%×1e4
    r, c = _run(monkeypatch, dict(EXT), _ptm(entry_stop_loss=2480, entry_take_profit=2100))
    assert c["amend"] == [(None, 2100.0)]
    assert any("单笔风险" in n and "弃用" in n for n in r["notes"])


def test_keep_same_trigger_switch_off(monkeypatch):
    from desk import okx_demo
    monkeypatch.setattr(okx_demo, "instrument", lambda inst: {"ctVal": "0.1"})
    orig = settings.load_config

    def off():
        c = orig(); c["manager"]["tm_pending_stop_by_tm"] = False; return c
    monkeypatch.setattr(settings, "load_config", off)
    r, c = _run(monkeypatch, dict(EXT), _ptm(entry_stop_loss=2480, entry_take_profit=2100))
    assert c["amend"] == [(None, 2100.0)]


def test_position_stop_still_tighten_only():
    src = open(manager.__file__, encoding="utf-8").read()
    assert "new_stop 只能收紧" in src


# ---------- 6. 来源标签 ----------
def test_session_label_from_decision_id(monkeypatch):
    monkeypatch.setattr(settings, "SESSION", "morning")
    assert demo_ws.session_label("ops:tm_reapply", None, "D-20261030-E-BTC") == "20:30晚场（人工运维重挂）"
    assert demo_ws.session_label("daily", None, "D-20261030-E-SOL") == "20:30晚场"
    assert demo_ws.session_label("daily", None, "D-20261030-BTC") == "8点早场"
    assert demo_ws.session_label("post_stop:T", None, "D-20261030-E-BTC") == "止损后再讨论"
    assert demo_ws.session_label("daily") == "8点早场"
    ev = demo_ws.placed_event({"inst_id": "BTC-USDT-SWAP", "side": "short", "decision_id": "D-20261030-E-BTC",
                               "source": "ops:tm_reapply", "algoId": "1"})
    assert "20:30晚场" in ev["message"] and "8点早场" not in ev["message"]
