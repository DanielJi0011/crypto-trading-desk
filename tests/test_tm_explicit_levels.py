"""2026-10-31：保留挂单改点纯函数（放宽止损需过硬护栏）；挂单提醒来源标签按决策号场次。离线。
具体点位必填现由交易经理下单工具校验负责（见 test_tm_order_tool）。"""
import pytest

from desk import demo_ws, settings, sizing


@pytest.fixture
def cfg():
    return settings.load_config()


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
