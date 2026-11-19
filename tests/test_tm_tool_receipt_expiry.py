"""feat/report-tool-sizing：下单工具预检回执（张数/保证金/止损风险/占权益）、定仓心算口径与基准示例、keep_expiry。全部离线。"""
import datetime as dt

import pytest

from desk import settings, tm_order_tool as T
from tests.test_tm_order_tool import env, _go, OTC, INV, PEND, EX, POS  # noqa: F401
from tests.test_tm_no_vote import desk_env  # noqa: F401


def _ex(hours=30.0):
    return {**EX, "expires": (settings.now() + dt.timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S%z"),
            "created": (settings.now() - dt.timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S%z")}


def test_receipt_returned_on_pass(env):
    m, calls, box = env
    rec = _go(m, box, [OTC(**PEND)])
    r = rec["tm_order_tool"]["receipt"]
    assert r and r["sz"] and r["notional"] > 0 and r["margin"] > 0
    qty = float(r["sz"])   # ctVal=1
    assert r["stop_loss_usd"] == pytest.approx(qty * 3.0, abs=0.01) and r["risk_pct_equity"] > 0
    z = rec["aggregate"]["sizing"]
    assert z["stop_loss_usd"] == pytest.approx(qty * 3.0, abs=0.01) and "risk_pct_equity" in z and "margin" in z


def test_risk_sizing_info_has_risk_usd(env):
    m, calls, box = env
    rec = _go(m, box, [OTC(**{**PEND, "margin_pct": None, "risk_scale": 0.5})])
    z = rec["aggregate"]["sizing"]
    assert z["how"] == "risk_sizing" and z.get("risk_usd") and z.get("stop_loss_usd") is not None


def test_reject_message_carries_receipt(env):
    m, calls, box = env
    box["guard"] = lambda qty, add: ["[POSITION] 超限"] if qty > 300 else []
    _go(m, box, [OTC(**{**PEND, "margin_pct": 40}), OTC(**{**PEND, "margin_pct": 20})])
    p = calls["prompts"][1]
    assert "[RANGE]" in p and "止损亏损约" in p and "占权益" in p and "保证金" in p


def test_prompt_formula_baseline_and_expiry_docs(env):
    m, calls, box = env
    _go(m, box, [OTC(**PEND)])
    p = calls["prompts"][0]
    assert "contracts come from risk_scale x the 1.5% per-trade risk base" in p
    assert "Current baseline for this coin" in p and "stop distance 2%, risk_scale 1" in p and "stop distance 4%, risk_scale 0.5" in p
    assert "counts from placement" in p and "keep_expiry=true" in p


def test_baseline_lines_without_cfg_only_formula():
    out = T.baseline_lines(None, {})
    assert len(out) == 1 and "do the mental math on this" in out[0]


def test_keep_expiry_keep_pending_no_expire_hours(env):
    m, calls, box = env
    box["ex"] = _ex()
    rec = _go(m, box, [OTC(**{**PEND, "action": "keep_pending", "expire_hours": None, "keep_expiry": True})])
    assert rec["final_action"] == "pending" and calls["expiry"] == [("OLD", None)]
    assert any("keep_expiry" in n and "不按 expire_hours 重算" in n for n in rec["notes"])
    assert "ORIGINAL placement time" in calls["prompts"][0]


def test_keep_expiry_ignores_given_expire_hours(env):
    m, calls, box = env
    box["ex"] = _ex()
    _go(m, box, [OTC(**{**PEND, "action": "keep_pending", "expire_hours": 5, "keep_expiry": True})])
    assert calls["expiry"] == [("OLD", None)]


def test_keep_pending_without_expiry_still_required(env):
    m, calls, box = env
    box["ex"] = _ex()
    _go(m, box, [OTC(**{**PEND, "action": "keep_pending", "expire_hours": None}), OTC(**{**PEND, "action": "keep_pending"})])
    assert "[MISSING]" in calls["prompts"][1] and "expire_hours" in calls["prompts"][1]


def test_keep_expiry_replace_uses_remaining_hours(env):
    m, calls, box = env
    box["ex"] = _ex(30.0)
    rec = _go(m, box, [OTC(**{**PEND, "action": "replace_pending", "trigger_px": 103.0, "stop_loss": 106.0, "take_profit": 97.0,
                              "expire_hours": None, "keep_expiry": True})])
    o = calls["place_trigger"][0][0][3]
    assert o["expire_hours"] == pytest.approx(30.0, abs=0.05)
    assert any("沿用原到期时刻" in n for n in rec["notes"])


def test_keep_expiry_rejected_for_new_pending(env):
    m, calls, box = env
    _go(m, box, [OTC(**{**PEND, "keep_expiry": True}), OTC(**PEND)])
    assert "[EXPIRY]" in calls["prompts"][1]


def test_keep_expiry_rejected_when_original_expired_or_missing(env):
    m, calls, box = env
    box["ex"] = {**EX, "expires": (settings.now() - dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S%z")}
    _go(m, box, [OTC(**{**PEND, "action": "keep_pending", "expire_hours": None, "keep_expiry": True}),
                 OTC(**{**PEND, "action": "keep_pending"})])
    assert "[EXPIRY]" in calls["prompts"][1]
    box["ex"], calls["prompts"][:] = {**EX, "expires": None}, []
    _go(m, box, [OTC(**{**PEND, "action": "replace_pending", "expire_hours": None, "keep_expiry": True}),
                 OTC(**{**PEND, "action": "replace_pending"})])
    assert "[EXPIRY]" in calls["prompts"][1] and "未登记" in calls["prompts"][1]


# ---------- 保留/重挂到期下限（2026-11-16 用户批准：顺延到下一场开场后 2 小时） ----------

def _floor(cfg=None):
    from desk import manager as _M
    return _M.next_session_expiry_floor(cfg or settings.load_config())


def _seed_reg(monkeypatch, ex):
    """把现有挂单 OLD 的到期登记进登记表（包裹真实 reg，只覆盖 OLD 一条），并记录 set_trigger_expiry 调用。"""
    from desk import demo_exec as DX
    real_reg = DX.reg
    store = {"expires": ex["expires"]}
    set_calls = []

    def fake_reg():
        r = real_reg()
        trig = dict(r.get("triggers") or {})
        e = dict(trig.get("OLD") or {})
        e.update({"expires": store["expires"], "created": ex.get("created")})
        trig["OLD"] = e
        r["triggers"] = trig
        return r

    def fake_set(inst, algo_id, new, why):
        set_calls.append((inst, algo_id, new, why))
        store["expires"] = new
        return True

    monkeypatch.setattr(DX, "reg", fake_reg)
    monkeypatch.setattr(DX, "set_trigger_expiry", fake_set)
    return set_calls


def test_next_session_floor_is_a_session_start_plus_2h():
    from desk import manager as M, sessions
    cfg = settings.load_config()
    f = M.next_session_expiry_floor(cfg)
    now = settings.now()
    assert f > now + dt.timedelta(hours=2)
    starts = {sessions.start_bj(cfg, s, (now.date() + dt.timedelta(days=o)).isoformat())
              for o in (0, 1, 2) for s in sessions.SESSIONS}
    assert f - dt.timedelta(hours=2) in starts


# ---------- 下限跳过正在跑的场次（2026-11-19 修正：早场 07:35 开跑、config 名义开场 08:00） ----------

_BJ8 = dt.timezone(dt.timedelta(hours=8))


def _floor_at(monkeypatch, when):
    from desk import manager as M
    monkeypatch.setattr(settings, "now", lambda: when)
    return M.next_session_expiry_floor(settings.load_config())


def test_floor_skips_running_morning_session(monkeypatch):
    # 2026-11-19 实测事故：早场 07:57 做保留决定，旧逻辑把 08:00 当下一场 → 下限 10:00，
    # 低于原到期 21:18 而静默失效。修正后下限 = 当日晚场 21:30 + 2h = 23:30。
    f = _floor_at(monkeypatch, dt.datetime(2026, 11, 19, 7, 57, tzinfo=_BJ8))
    assert f == dt.datetime(2026, 11, 19, 23, 30, tzinfo=_BJ8)


def test_floor_skips_running_evening_session(monkeypatch):
    # 晚场窗口 20:55（北京）起、名义开场 21:30：21:05 的保留决定，下限应落到次日早场 08:00 + 2h。
    f = _floor_at(monkeypatch, dt.datetime(2026, 11, 19, 21, 5, tzinfo=_BJ8))
    assert f == dt.datetime(2026, 11, 20, 10, 0, tzinfo=_BJ8)


def test_floor_inside_morning_window_after_start(monkeypatch):
    # 早场窗口内、名义开场之后（到期讨论等）：下一场仍是当日晚场，与旧逻辑一致。
    f = _floor_at(monkeypatch, dt.datetime(2026, 11, 19, 10, 30, tzinfo=_BJ8))
    assert f == dt.datetime(2026, 11, 19, 23, 30, tzinfo=_BJ8)


def test_floor_outside_windows_unchanged(monkeypatch):
    # 不在任何场次窗口内：行为与旧逻辑一致（下一场开场 + 2h）。
    f = _floor_at(monkeypatch, dt.datetime(2026, 11, 19, 15, 0, tzinfo=_BJ8))
    assert f == dt.datetime(2026, 11, 19, 23, 30, tzinfo=_BJ8)


def test_keep_floor_extends_registry_expiry(env, monkeypatch):
    from desk import okx_demo as X
    m, calls, box = env
    box["ex"] = _ex(1.0)   # 原到期在下一场下限之前（事故场景：保留后几十分钟就到期）
    set_calls = _seed_reg(monkeypatch, box["ex"])
    events = []
    monkeypatch.setattr(X, "log_event", lambda kind, **kw: events.append((kind, kw)))
    rec = _go(m, box, [OTC(**{**PEND, "action": "keep_pending", "expire_hours": None, "keep_expiry": True})])
    assert rec["final_action"] == "pending"
    assert len(set_calls) == 1 and set_calls[0][1] == "OLD"
    assert set_calls[0][2] == _floor().strftime("%Y-%m-%d %H:%M:%S%z")
    assert any(k == "expiry_extended_on_keep" and kw.get("algoId") == "OLD" for k, kw in events)
    assert any("到期顺延" in n and "下一场开场后 2 小时" in n for n in rec["notes"])


def test_keep_floor_leaves_later_expiry_untouched(env, monkeypatch):
    m, calls, box = env
    box["ex"] = _ex(48.0)   # 原到期远晚于下限 → 不动
    set_calls = _seed_reg(monkeypatch, box["ex"])
    rec = _go(m, box, [OTC(**{**PEND, "action": "keep_pending", "expire_hours": None, "keep_expiry": True})])
    assert rec["final_action"] == "pending"
    assert set_calls == []
    assert not any("到期顺延" in n for n in rec["notes"])


def test_keep_expiry_replace_floored_to_next_session(env):
    m, calls, box = env
    box["ex"] = _ex(1.0)   # 原到期在下一场下限之前 → 重挂单有效期按下限重算
    rec = _go(m, box, [OTC(**{**PEND, "action": "replace_pending", "trigger_px": 103.0, "stop_loss": 106.0, "take_profit": 97.0,
                              "expire_hours": None, "keep_expiry": True})])
    o = calls["place_trigger"][0][0][3]
    expect = (_floor() - settings.now()).total_seconds() / 3600
    assert o["expire_hours"] == pytest.approx(expect, abs=0.1) and o["expire_hours"] > 2.0
    assert any("顺延" in n and "下一场开场后 2 小时" in n for n in rec["notes"])


def test_remaining_hours_parsing():
    now = settings.now()
    assert T.remaining_hours((now + dt.timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S%z")) == pytest.approx(3, abs=0.01)
    assert T.remaining_hours((now + dt.timedelta(hours=3)).isoformat()) == pytest.approx(3, abs=0.01)
    assert T.remaining_hours(None) is None and T.remaining_hours("bad") is None
    assert T.remaining_hours((now - dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S%z")) is None
