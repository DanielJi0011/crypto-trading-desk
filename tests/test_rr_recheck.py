"""开仓后 RR 复核（desk/rr_recheck.py）：基线不追溯、按实际成交价算 RR、坏 RR 只触发一次再讨论、
长边镜像、市价兜底回归（2026-11-11 ETH 计划 2075/2126/1985、成交 2055.74 → RR≈1.007）、
启动失败重试、HALT 递延、缺止盈跳过、开关关闭、min_rr 读配置；以及 monitor 对 rr_recheck 的
冷却豁免 + detail 存档/清理、rediscuss 的 rr_recheck 简报。全部离线（注入 positions/sltp/schedule）。"""
import copy
import datetime as dt

import pytest

from desk import rr_recheck, settings


def pos(inst="ETH-USDT-SWAP", pos_id="P1", c_time="1700000000000", pos_side="short", sz="84.2", avg="2055.74"):
    return {"posId": pos_id, "cTime": c_time, "instId": inst, "posSide": pos_side, "pos": sz, "avgPx": avg}


ETH_PLAN = {"planned_entry": 2075.0, "limit_px": 2075.0, "planned_sl": 2126.0, "planned_tp": 1985.0,
            "planned_rr": pytest.approx(1.76)}


@pytest.fixture()
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(rr_recheck, "PATH", tmp_path / "rr_recheck.json")
    cfg = settings.load_config()
    calls = []

    def schedule(cfg_, trade):
        calls.append(trade)
        return "已启动"
    return cfg, calls, schedule


def _eth(sltp=(2126.0, 1985.0), plan=ETH_PLAN):
    return {"sltp": lambda inst, ps: sltp, "planned": lambda inst, side, meta: plan}


def test_baseline_never_retrofires(env):
    """首次运行：已有持仓（就是今天 ETH 那笔坏 RR）只建基线，永不补触发。"""
    cfg, calls, schedule = env
    kw = _eth()
    lines = rr_recheck.check(cfg, positions=[pos()], schedule=schedule, **kw)
    assert lines and "基线" in lines[0] and not calls
    rr_recheck.check(cfg, positions=[pos()], schedule=schedule, **kw)   # 后续轮次也不触发
    rr_recheck.check(cfg, positions=[pos()], schedule=schedule, **kw)
    assert not calls
    st = settings.read_json(rr_recheck.PATH, {})
    assert list(st["opens"].values())[0]["status"] == "baseline"


def test_good_rr_no_fire(env):
    cfg, calls, schedule = env
    rr_recheck.check(cfg, positions=[], schedule=schedule)   # 基线（空）
    btc = pos(inst="BTC-USDT-SWAP", pos_id="P2", pos_side="long", sz="1.5", avg="100")
    lines = rr_recheck.check(cfg, positions=[btc], schedule=schedule,
                             sltp=lambda i, p: (95.0, 110.0), planned=lambda i, s, m: {})
    assert not calls and lines == []
    st = settings.read_json(rr_recheck.PATH, {})
    rec = st["opens"]["P2:1700000000000"]
    assert rec["status"] == "ok" and rec["rr"] == pytest.approx(2.0)


def test_bad_rr_eth_regression_fires_once_with_facts(env):
    """回归：计划 2075/2126/1985（RR 1.76），市价兜底成交 2055.74 → 实际 RR 70.74/70.26≈1.007 → 触发一次。"""
    cfg, calls, schedule = env
    rr_recheck.check(cfg, positions=[], schedule=schedule)
    kw = _eth()
    lines = rr_recheck.check(cfg, positions=[pos()], schedule=schedule, **kw)
    assert len(calls) == 1 and lines
    tr = calls[0]
    assert tr["reason"] == "rr_recheck" and tr["inst_id"] == "ETH-USDT-SWAP" and tr["trade_id"] == "DEMO-P1"
    d = tr["detail"]
    assert d["fill"] == 2055.74 and d["sl"] == 2126.0 and d["tp"] == 1985.0
    assert d["rr"] == pytest.approx(70.74 / 70.26, abs=1e-3) and d["rr"] < 1.5
    assert d["planned_entry"] == 2075.0 and d["planned_rr"] == pytest.approx(1.76, abs=0.01)
    assert d["sl_dist"] == pytest.approx(70.26) and d["tp_dist"] == pytest.approx(70.74)
    # TM 选择保留（状态 fired）后，反复巡检不再触发
    rr_recheck.check(cfg, positions=[pos()], schedule=schedule, **kw)
    rr_recheck.check(cfg, positions=[pos()], schedule=schedule, **kw)
    assert len(calls) == 1
    st = settings.read_json(rr_recheck.PATH, {})
    assert st["opens"]["P1:1700000000000"]["status"] == "fired"


def test_long_mirror_bad_rr_fires(env):
    cfg, calls, schedule = env
    rr_recheck.check(cfg, positions=[], schedule=schedule)
    sol = pos(inst="SOL-USDT-SWAP", pos_id="P3", pos_side="long", sz="10", avg="200")
    rr_recheck.check(cfg, positions=[sol], schedule=schedule,
                     sltp=lambda i, p: (190.0, 205.0), planned=lambda i, s, m: {})
    assert len(calls) == 1 and calls[0]["detail"]["rr"] == pytest.approx(0.5)


def test_spawn_failure_stays_pending_and_retries(env):
    cfg, calls, schedule = env
    rr_recheck.check(cfg, positions=[], schedule=schedule)
    kw = _eth()
    rr_recheck.check(cfg, positions=[pos()], schedule=lambda c, t: "启动失败", **kw)
    st = settings.read_json(rr_recheck.PATH, {})
    assert st["opens"]["P1:1700000000000"]["status"] == "pending"
    rr_recheck.check(cfg, positions=[pos()], schedule=schedule, **kw)   # 下轮重试成功
    assert len(calls) == 1
    st = settings.read_json(rr_recheck.PATH, {})
    assert st["opens"]["P1:1700000000000"]["status"] == "fired"


def test_halt_defers_then_fires(env):
    cfg, calls, schedule = env
    rr_recheck.check(cfg, positions=[], schedule=schedule)
    kw = _eth()
    rr_recheck.check(cfg, positions=[pos()], schedule=schedule, halt=True, **kw)
    assert not calls
    rr_recheck.check(cfg, positions=[pos()], schedule=schedule, halt=False, **kw)
    assert len(calls) == 1


def test_missing_tp_skipped_after_grace(env):
    cfg, calls, schedule = env
    rr_recheck.check(cfg, positions=[], schedule=schedule)
    t0 = settings.now()
    kw = {"sltp": lambda i, p: (2126.0, None), "planned": lambda i, s, m: {}}
    rr_recheck.check(cfg, positions=[pos()], schedule=schedule, now=t0, **kw)
    assert not calls   # 宽限期内：不触发也不跳过（等保护单重挂）
    st = settings.read_json(rr_recheck.PATH, {})
    assert st["opens"]["P1:1700000000000"]["status"] == "pending"
    t1 = t0 + dt.timedelta(seconds=rr_recheck.GRACE_SECONDS + 60)
    rr_recheck.check(cfg, positions=[pos()], schedule=schedule, now=t1, **kw)
    assert not calls
    st = settings.read_json(rr_recheck.PATH, {})
    rec = st["opens"]["P1:1700000000000"]
    assert rec["status"] == "skipped" and rec["note"] == "missing_sltp"


def test_position_closed_before_check_is_gone(env):
    cfg, calls, schedule = env
    rr_recheck.check(cfg, positions=[], schedule=schedule)
    rr_recheck.check(cfg, positions=[pos()], schedule=schedule, halt=True, **_eth())   # 先登记 pending
    rr_recheck.check(cfg, positions=[], schedule=schedule, **_eth())                    # 已平 → gone
    assert not calls
    st = settings.read_json(rr_recheck.PATH, {})
    assert st["opens"]["P1:1700000000000"]["status"] == "gone"


def test_switch_off_does_nothing(env):
    cfg, calls, schedule = env
    cfg = copy.deepcopy(cfg)
    cfg["pending"]["rr_recheck_enabled"] = False
    assert rr_recheck.check(cfg, positions=[pos()], schedule=schedule, **_eth()) == []
    assert not calls and not rr_recheck.PATH.exists()


def test_min_rr_comes_from_config(env):
    cfg, calls, schedule = env
    cfg = copy.deepcopy(cfg)
    cfg["risk"]["min_rr_after_adjust"] = 1.0   # ETH 实际 RR≈1.007 ≥ 1.0 → 不触发
    rr_recheck.check(cfg, positions=[], schedule=schedule)
    rr_recheck.check(cfg, positions=[pos()], schedule=schedule, **_eth())
    assert not calls
    st = settings.read_json(rr_recheck.PATH, {})
    assert st["opens"]["P1:1700000000000"]["status"] == "ok"


# ---------------- monitor / rediscuss 衔接
@pytest.fixture()
def mon(monkeypatch, tmp_path):
    from desk import monitor
    monkeypatch.setattr(monitor, "REDISCUSS", tmp_path / "rediscuss.json")
    calls = []
    monkeypatch.setattr(monitor, "SPAWN", lambda args, lf: calls.append(args))
    return settings.load_config(), monitor, calls


def test_monitor_rr_recheck_bypasses_cooldown_and_stores_detail(mon):
    cfg, monitor, calls = mon
    now = settings.now_str()
    settings.write_json(monitor.REDISCUSS, {"ETH-USDT-SWAP": {"events": [], "last": now}})   # 冷却正活跃
    tr_sl = {"inst_id": "ETH-USDT-SWAP", "trade_id": "DEMO-999", "reason": "sl"}
    assert monitor.schedule_rediscuss(cfg, tr_sl) == "冷却中" and not calls          # 旧行为不变
    detail = {"fill": 2055.74, "rr": 1.007, "min_rr": 1.5}
    tr_rr = {"inst_id": "ETH-USDT-SWAP", "trade_id": "DEMO-P1", "reason": "rr_recheck", "detail": detail}
    assert monitor.schedule_rediscuss(cfg, tr_rr) == "已启动" and len(calls) == 1    # rr_recheck 豁免冷却
    assert "--reason" in calls[0] and calls[0][calls[0].index("--reason") + 1] == "rr_recheck"
    st = settings.read_json(monitor.REDISCUSS, {})
    assert st["ETH-USDT-SWAP"]["details"]["DEMO-P1"] == detail                       # 事实包已存档
    assert monitor.complete_rediscuss("ETH-USDT-SWAP", "DEMO-P1") is True            # 完成后清理
    st = settings.read_json(monitor.REDISCUSS, {})
    assert "DEMO-P1" not in (st["ETH-USDT-SWAP"].get("details") or {})
    assert "DEMO-P1" in st["ETH-USDT-SWAP"]["events"]


def test_reason_brief_rr_recheck_carries_facts():
    from desk import rediscuss
    d = {"planned_entry": 2075.0, "limit_px": 2075.0, "planned_rr": 1.76, "fill": 2055.74,
         "sl": 2126.0, "sl_dist": 70.26, "tp": 1985.0, "tp_dist": 70.74, "rr": 1.007, "min_rr": 1.5}
    b = rediscuss.reason_brief("rr_recheck", "DEMO-P1", d)
    for s in ("2075", "2055.74", "2126", "1985", "1.007", "1.76"):
        assert s in b
    assert "止损" in rediscuss.reason_brief("sl", "DEMO-1")          # 旧简报不受影响
    assert rediscuss.REASON_CN["rr_recheck"] == "盈亏比复核"
