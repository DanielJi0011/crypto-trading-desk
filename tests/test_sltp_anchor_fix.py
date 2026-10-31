"""2026-10-30 feat/sltp-anchor-fix：止损锚定实际入场价、RR 不足先收紧止损、止盈封顶；各开关 false 恢复旧行为。"""
import copy

import pytest

from desk import settings, sizing

ATR = 1987.61
LAST = 77211.7


def _cfg(**sz):
    cfg = copy.deepcopy(settings.load_config())
    cfg.setdefault("sizing", {}).update(sz)
    return cfg


def _fin(cfg, side, entry, role_stop, role_tp, atr=ATR):
    rc = sizing.rcfg(cfg)
    bs, bt, _ = sizing.stop_take(side, entry, atr, rc)
    return sizing.finalize_sl_tp(side, entry, bs, bt, role_stop, role_tp, rc["min_rr_after_adjust"],
                                 **sizing.anchor_opts(cfg, rc, atr, entry))


def test_market_entry_far_stop_tightened_to_cap():
    # 止损离入场很远，但止盈足够让 RR≥1.5：只收紧到 1.25×基准
    base = 1.5 * ATR
    f = _fin(_cfg(), "short", LAST, 82300.0, LAST - 2.3 * base)
    assert f["stop"] == pytest.approx(LAST + 1.25 * base)
    assert any("收紧至" in n for n in f["notes"])


def test_rr_short_tightens_stop_first_then_tp():
    base = 1.5 * ATR
    f = _fin(_cfg(), "short", LAST, 82300.0, 76200.0)
    assert f["stop"] == pytest.approx(LAST + base)          # 先收紧到基准距离
    assert f["take_profit"] == pytest.approx(LAST - 1.5 * base)
    assert f["rr"] == pytest.approx(1.5)
    assert any("先把止损收紧" in n for n in f["notes"])


def test_tp_capped():
    base = 1.5 * ATR
    f = _fin(_cfg(), "long", 100000.0, None, 100000.0 + 5 * base)
    assert f["take_profit"] == pytest.approx(100000.0 + 1.2 * 2.0 * base)


def test_rr_still_short_after_cap_recorded_not_silent():
    # 关闭锚定与先收紧止损，只开封顶：止损 3×基准，外推需 4.5×基准 > 封顶 2.4×基准 → 如实记录
    base = 1.5 * ATR
    cfg = _cfg(sltp_anchor_entry=False, sltp_rr_tighten_stop_first=False)
    f = _fin(cfg, "long", 100000.0, 100000.0 - 3 * base, 100000.0 + base)
    assert f["take_profit"] == pytest.approx(100000.0 + 2.4 * base)
    assert f["rr_short"] == pytest.approx(0.8) and any("如实记录" in n for n in f["notes"])


def test_switches_off_restore_old():
    cfg = _cfg(sltp_anchor_entry=False, sltp_rr_tighten_stop_first=False, sltp_tp_cap=False)
    f = _fin(cfg, "short", LAST, 82300.0, 76200.0)
    assert f["stop"] == 82300.0
    assert f["take_profit"] == pytest.approx(LAST - 1.5 * (82300.0 - LAST))   # 旧：只外推止盈
    old = sizing.finalize_sl_tp("short", LAST, None, None, 82300.0, 76200.0, 1.5)
    assert (f["stop"], f["take_profit"]) == (old["stop"], old["take_profit"])

