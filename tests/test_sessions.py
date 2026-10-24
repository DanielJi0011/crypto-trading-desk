"""晚场（纽约 08:30）验收：zoneinfo 夏令时/冬令时换算、标记与日志、决策ID、x_feed 路径、打分独立样本、下单不限量、evening_missing。离线，不调用 AI，不发 webhook。"""
import datetime as dt
import json
import os
import subprocess
from pathlib import Path

import pandas as pd
import pytest

from desk import chain_health, scoring, sessions, settings

ROOT = Path(__file__).resolve().parents[1]
BJ = dt.timezone(dt.timedelta(hours=8))


def _hm(t):
    return t.strftime("%H:%M")


def test_evening_dst_2026_10_23():
    cfg = settings.load_config()
    a, b = sessions.window_bj(cfg, "evening", "2026-10-23")
    assert (_hm(a), _hm(b)) == ("20:25", "22:30")
    assert _hm(sessions.start_bj(cfg, "evening", "2026-10-23")) == "20:30"
    assert _hm(sessions.missing_at(cfg, "evening", "2026-10-23")) == "21:30"


def test_evening_winter_2026_11_02():
    cfg = settings.load_config()
    a, b = sessions.window_bj(cfg, "evening", "2026-11-02")
    assert (_hm(a), _hm(b)) == ("21:25", "23:30")
    assert _hm(sessions.start_bj(cfg, "evening", "2026-11-02")) == "21:30"
    assert _hm(sessions.missing_at(cfg, "evening", "2026-11-02")) == "22:30"


def test_morning_unchanged_and_keys():
    cfg = settings.load_config()
    a, b = sessions.window_bj(cfg, "morning", "2026-11-02")
    assert (_hm(a), _hm(b)) == ("07:55", "12:00")
    assert sessions.key("2026-10-23", "morning") == "2026-10-23"
    assert sessions.key("2026-10-23", "evening") == "2026-10-23-evening"
    assert sessions.did_prefix("2026-10-23", "evening") + "BTC" == "D-20261023-E-BTC"
    assert sessions.did_prefix("2026-10-23", None) + "BTC" == "D-20261023-BTC"


def _env(tmp, now):
    s = tmp / "stub.sh"; s.write_text(f"echo \"$@\" >> {tmp}/entered.txt\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OKX_", "TA_NOTIFY_WEBHOOK"))}
    env.update(TA_DESK_HOME=str(tmp), TA_DAILY_RUNNER=f"bash {s}", TA_NOTIFY_DISABLED="1", TA_EVENT_SOURCE="test", TA_DAILY_NOW=now)
    return env


@pytest.mark.parametrize("day,now,enter", [("2026-10-23", "20:24", False), ("2026-10-23", "20:30", True),
                                           ("2026-10-23", "22:31", False), ("2026-11-02", "20:30", False),
                                           ("2026-11-02", "23:30", True)])
def test_run_daily_evening_window_and_marker(tmp_path, day, now, enter):
    r = subprocess.run(["bash", str(ROOT / "run_daily.sh"), "--session", "evening", "--date", day], env=_env(tmp_path, now),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0
    assert (tmp_path / "entered.txt").exists() == enter
    assert (tmp_path / "state" / "daily_done" / f"{day}-evening").exists() == enter
    assert not (tmp_path / "state" / "daily_done" / day).exists()          # 不占用早场标记
    assert list((tmp_path / "logs").glob("run-*-evening.log"))
    if enter:
        assert "--session evening" in (tmp_path / "entered.txt").read_text()


def test_run_daily_evening_failed_marker(tmp_path):
    env = _env(tmp_path, "20:30")
    (tmp_path / "stub.sh").write_text("exit 4\n")
    r = subprocess.run(["bash", str(ROOT / "run_daily.sh"), "--session", "evening", "--date", "2026-10-23"], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 4
    assert (tmp_path / "state" / "daily_failed" / "2026-10-23-evening").read_text().strip() == "4"
    assert not (tmp_path / "state" / "daily_failed" / "2026-10-23").exists()


def test_order_cap_zero_means_unlimited():
    cfg = settings.load_config()
    assert int(cfg["guardrails"]["max_orders_per_day"]) == 0
    for p in ("desk/demo_exec.py", "desk/ledger.py"):
        assert "lim > 0 and n >= lim" in (ROOT / p).read_text()


def test_x_feed_path_by_session():
    assert str(Path("state/x_feed") / sessions.key("2026-10-23", "evening")).endswith("x_feed/2026-10-23-evening")


@pytest.fixture
def clean():
    ps = (scoring.STANCES, scoring.SCORES, scoring.WEIGHTS, scoring.WEIGHTS_SHADOW, scoring.WEIGHT_HIST)
    for p in ps:
        p.exists() and p.unlink()
    yield
    for p in ps:
        p.exists() and p.unlink()


def test_evening_scoring_independent_from_decision_time(clean, monkeypatch):
    days = pd.date_range("2026-01-01", periods=400)
    d = pd.DataFrame({"Date": days, "Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0})
    hrs = pd.date_range("2026-10-20", periods=24 * 12, freq="h")
    close = [100.0 if t < pd.Timestamp("2026-10-23 13:00") else 110.0 for t in hrs]   # 晚场决策（UTC 12:30）后才涨
    hb = pd.DataFrame({"Date": hrs, "Open": close, "High": [c * 1.001 for c in close], "Low": [c * 0.999 for c in close], "Close": close})
    monkeypatch.setattr(scoring, "DAILY", lambda inst: d)
    monkeypatch.setattr(scoring, "HOURLY", lambda inst: hb)
    base = {"inst_id": "BTC-USDT-SWAP", "role": "market", "direction": "bullish", "confidence": 0.6, "target": None,
            "stop": None, "horizon_days": 2, "ref_px": 100.0, "regime": "震荡", "date": "2026-10-23"}
    settings.append_jsonl(scoring.STANCES, {**base, "id": "D-20261023-E-BTC:market:BTC-USDT-SWAP",
                                            "decision_id": "D-20261023-E-BTC", "time": "2026-10-23 20:30:00+0800", "session": "evening"})
    cfg = settings.load_config()
    early = dt.datetime(2026, 10, 25, 20, 0, tzinfo=BJ)                    # 不足 48 小时：不打分
    assert scoring.score_due(cfg, now=early) == []
    later = dt.datetime(2026, 10, 25, 22, 0, tzinfo=BJ)
    new = scoring.score_due(cfg, now=later)
    assert len(new) == 1 and new[0]["session"] == "evening" and new[0]["hit"] is True
    assert new[0]["ret_pct"] == pytest.approx(10.0)
    assert scoring.score_due(cfg, now=later) == []                           # 按观点 id 去重，不重复打分


def test_evening_missing_once_per_day(tmp_path, monkeypatch):
    st = tmp_path / "state"; st.mkdir()
    monkeypatch.setattr(settings, "STATE", st)
    monkeypatch.setattr(chain_health, "PATH", st / "chain_health.json")
    monkeypatch.setattr(chain_health, "problems", lambda cfg, ts: {})
    for k in ("KILL_PATH", "HALT_PATH"):
        monkeypatch.setattr(settings, k, st / k)
    monkeypatch.setattr(settings, "LEGACY_KILL_PATH", st / "ledger" / "KILL")
    (st / "daily_done").mkdir(); (st / "daily_done" / "2026-10-23").write_text("x")
    sent = []
    n = lambda ev: sent.append(ev["kind"])
    cfg = settings.load_config()
    chain_health.run(cfg, dt.datetime(2026, 10, 23, 21, 29, tzinfo=BJ), n)
    assert "evening_missing" not in sent
    chain_health.run(cfg, dt.datetime(2026, 10, 23, 21, 31, tzinfo=BJ), n)
    chain_health.run(cfg, dt.datetime(2026, 10, 23, 22, 0, tzinfo=BJ), n)
    assert sent.count("evening_missing") == 1
    from desk import demo_ws
    assert "evening_missing" in demo_ws.NOTIFY_KINDS
