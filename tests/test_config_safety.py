"""D1 第 4 点：配置安全校验；R1 上限部分：单笔止损亏损 ≤ risk_per_trade_pct×权益。离线。"""
import copy
from pathlib import Path

import pytest
import yaml

from desk import settings, config_safety as CS, demo_exec as DX, okx_demo as X

ROOT = Path(__file__).resolve().parents[1]


def _raw():
    return yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


def test_current_config_passes():
    CS.validate(_raw())
    settings.load_config()


def test_unenforced_safety_key_fails_startup(tmp_path, monkeypatch):
    raw = _raw(); raw["guardrails"]["allow_short_squeeze"] = False
    p = tmp_path / "c.yaml"; p.write_text(yaml.safe_dump(raw, allow_unicode=True))
    monkeypatch.setattr(settings, "CONFIG", p)
    with pytest.raises(CS.ConfigSafetyError, match="allow_short_squeeze"):
        settings.load_config()


@pytest.mark.parametrize("mut,msg", [
    (lambda c: c["guardrails"].update(leverage=3), "矛盾"),
    (lambda c: c["guardrails"].pop("allow_leverage"), "显式配置"),
    (lambda c: c["guardrails"].update(require_stop_loss=False), "require_stop_loss"),
    (lambda c: c["guardrails"].update(allow_withdrawal=True), "allow_withdrawal"),
    (lambda c: c["risk"].update(max_daily_loss_pct=2.5), "超出"),
    (lambda c: c.update(mode="live"), "mode"),
])
def test_invalid_combinations(mut, msg):
    raw = _raw(); mut(raw)
    with pytest.raises(CS.ConfigSafetyError, match=msg):
        CS.validate(raw)


def test_q11_not_resolved_by_deleting_keys():
    g = _raw()["guardrails"]
    assert g["leverage"] == 1 and g["allow_leverage"] is False      # 两项保留，且被代码执行
    assert CS.max_open_leverage(_raw()) == 1


def test_every_enforced_key_is_read_by_declared_files():
    for name, files in CS.ENFORCED.items():
        key = name.split(".", 1)[1]
        assert any(key in (ROOT / f).read_text(encoding="utf-8") for f in files), f"{name} 未在 {files} 中读取"
        assert any(f'"{key}"' in (ROOT / f).read_text(encoding="utf-8") or f"'{key}'" in (ROOT / f).read_text(encoding="utf-8")
                   or f".{key}" in (ROOT / f).read_text(encoding="utf-8") for f in files), name


def test_allowed_ord_types_enforced(monkeypatch, tmp_path):
    monkeypatch.setattr(X, "EVENTS", tmp_path / "ev.jsonl")
    calls = []
    monkeypatch.setattr(X, "_request", lambda m, p, params=None, body=None: calls.append(body) or {"code": "0", "data": [{}]})
    X.post("/api/v5/trade/order", {"ordType": "market"})
    with pytest.raises(X.DemoError):
        X.post("/api/v5/trade/order", {"ordType": "post_only"})
    assert len(calls) == 1


INSTR = {"ctVal": "0.01", "lotSz": "0.01", "minSz": "0.01", "tickSz": "0.1"}


def test_risk_clip_and_reject(monkeypatch, tmp_path):
    from desk import equity_guard
    monkeypatch.setattr(equity_guard, "MARKS", tmp_path / "m.json")
    monkeypatch.setattr(X, "EVENTS", tmp_path / "ev.jsonl")
    monkeypatch.setattr(X, "_inst_cache", {"BTC-USDT-SWAP": INSTR})
    monkeypatch.setattr(DX, "today_realized", lambda: 0.0); monkeypatch.setattr(DX, "today_orders", lambda: 0)
    cfg = settings.load_config()
    cfg["risk"]["risk_per_trade_pct"] = 0.0075   # 显式旧值（2026-10-30 起配置为 0.015，见 test_risk_1p5.py）
    bk = {"equity": 100000, "live_equity": 100000, "positions": [], "triggers": [], "exposure": 0, "pending_entries": []}
    # 名义 20000、止损 5% → 亏损 1000 > 750 → 削到 0.15 币（名义 15000，亏损 750）
    sz = DX.clip_to_risk(cfg, bk, "BTC-USDT-SWAP", "20", 100000, 95000)
    assert float(sz) * 0.01 * 5000 <= 750 + 1e-6 and float(sz) == pytest.approx(15)
    r, ch = DX.check_open(cfg, bk, "BTC-USDT-SWAP", 0.2, 100000, 95000, "long", 1)
    assert any("[RISK_PER_TRADE]" in x for x in r)
    r, ch = DX.check_open(cfg, bk, "BTC-USDT-SWAP", 0.15, 100000, 95000, "long", 1)
    assert not any("[RISK_PER_TRADE]" in x for x in r) and ch["stop_loss_usd"] <= ch["risk_cap_usd"] + 1e-6


@pytest.mark.parametrize("sz,px,sl", [("100", 100000, 99000), ("3", 100000, 90000), ("50", 2000, 2100), ("1", 100, 85)])
def test_risk_clip_property(monkeypatch, tmp_path, sz, px, sl):
    monkeypatch.setattr(X, "EVENTS", tmp_path / "ev.jsonl")
    monkeypatch.setattr(X, "_inst_cache", {"BTC-USDT-SWAP": INSTR})
    cfg = settings.load_config()
    cfg["risk"]["risk_per_trade_pct"] = 0.0075   # 显式旧值
    out = DX.clip_to_risk(cfg, {"equity": 100000}, "BTC-USDT-SWAP", sz, px, sl)
    assert float(out or 0) * 0.01 * abs(px - sl) <= 750 + 1e-6 and float(out or 0) <= float(sz)
