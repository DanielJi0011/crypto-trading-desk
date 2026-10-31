"""2026-10-30 入场方案修复的保留部分：【立场】对现有挂单字段解析、保留挂单改点纯函数、模拟 OKX 改附带止盈止损。"""
from desk import settings, sizing, stances
from desk import demo_exec as DX



def test_parse_pending_action():
    for word, v in (("保留", "keep"), ("改价", "reprice"), ("撤销", "cancel"), ("改现价", "now")):
        s = stances.parse_stance(f"x\n【立场】方向=看空；目标价=90；止损价=110；置信度=0.6；周期=3天；对现有挂单={word}")
        assert s and s.get("pending_action") == v, word
    s = stances.parse_stance("x\n【立场】方向=看空；目标价=90；止损价=110；置信度=0.6；周期=3天")
    assert s.get("pending_action") is None


# ---------- 改动 3：纯函数 ----------
def test_keep_sltp_rules():
    f = sizing.pending_keep_sltp
    r = f("short", 2334, 2432, 2185, 2432, 2160, 2290)          # ETH 实例：只改止盈
    assert r["changed"] and r["take_profit"] == 2160 and r["stop_loss"] == 2432 and r["rr"] >= 1.5
    r = f("short", 79900, 81950, 76400, 82300, 76000, 77200)    # 止损放宽 → 保持；止盈可改
    assert r["stop_loss"] == 81950 and r["take_profit"] == 76000 and any("放宽" in x for x in r["reasons"])
    r = f("short", 100, 104, 95, None, 99.95, 100)              # 止盈距现价不足 0.3% 且 RR 不够
    assert not r["changed"]
    r = f("short", 100, 104, 95, 103, 101, 98)                  # 止盈不在盈利侧
    assert r["take_profit"] == 95 and r["stop_loss"] == 103
    r = f("short", 100, 104, 95, 101, 99, 98)                   # RR<1.5 → 都保持
    assert not r["changed"] and any("RR" in x for x in r["reasons"])


EXT = {"algoId": "OLD", "side": "short", "trigger_px": 2334.0, "condition": "gte", "stop_loss": 2432.0,
       "take_profit": 2185.0, "sz": "3", "expires": ""}


def test_fake_okx_amend_attach(monkeypatch):
    from tests.fake_okx import FakeOKX
    f = FakeOKX().install(monkeypatch)
    f.algos.append({"algoId": "OLD", "attachAlgoOrds": [{"slTriggerPx": "2432", "tpTriggerPx": "2185"}]})
    monkeypatch.setattr(DX, "reg", lambda: {"positions": {}, "triggers": {}})
    monkeypatch.setattr(DX, "save_reg", lambda r: None)
    from desk import okx_demo
    monkeypatch.setattr(okx_demo, "log_event", lambda *a, **k: None)
    res = DX.amend_trigger_sltp(settings.load_config(), "ETH-USDT-SWAP", dict(EXT), None, 2160.0)
    assert res["how"] == "amend" and f.algos[0]["attachAlgoOrds"][0]["tpTriggerPx"] == "2160.00"
