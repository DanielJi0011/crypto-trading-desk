"""O3：事件 source 标记；每日下单数只统计 strategy 与 rediscuss。"""
import json
from pathlib import Path

from desk import okx_demo as X, demo_exec as DX, settings

ROOT = Path(__file__).resolve().parents[1]


def test_source_tag_and_count(tmp_path, monkeypatch):
    monkeypatch.setattr(X, "EVENTS", tmp_path / "ev.jsonl")
    X.log_event("x")
    assert json.loads((tmp_path / "ev.jsonl").read_text())["source"] == "test"   # conftest 设为 test
    rows = [("strategy", "open_ok"), ("rediscuss", "trigger_placed"), ("ops", "open_ok"), ("test", "trigger_placed"), (None, "open_ok")]
    with open(tmp_path / "ev.jsonl", "w") as f:
        for src, ev in rows:
            r = {"time": settings.now_str(), "event": ev, **({"source": src} if src else {})}
            f.write(json.dumps(r) + "\n")
    assert DX.today_orders() == 3   # strategy + rediscuss + 旧事件（无 source 保守计入）


def test_set_source(monkeypatch):
    monkeypatch.setenv("TA_EVENT_SOURCE", "rediscuss")
    assert X.event_source() == "rediscuss"
    monkeypatch.setenv("TA_EVENT_SOURCE", "weird")
    assert X.event_source() == "ops"


def test_all_ops_scripts_tag_source():
    for f in ("btc_demo_execute", "eth_demo_backfill", "reprice_triggers_limit", "btc_rediscuss_demo", "demo_smoke", "rerun_coin"):
        t = (ROOT / "scripts" / f"{f}.py").read_text(encoding="utf-8")
        assert 'set_event_source("ops")' in t or 'set_event_source("test")' in t, f
    assert 'set_event_source("rediscuss")' in (ROOT / "desk" / "rediscuss.py").read_text(encoding="utf-8")
