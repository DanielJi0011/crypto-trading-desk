"""R5（第一批）：当日亏损与回撤护栏，计入浮亏，任一数据读取失败即拒绝开仓（fail-closed）。

状态文件 state/equity_marks.json：
  day             北京时间日期（YYYY-MM-DD）
  day_start_equity 该日首次读取到的实时总权益（含浮盈亏）
  peak_equity     历史最高实时总权益
  daily_halt_day  已发过 daily_loss_halt 的日期（每日一次）
规则（参数见 config.yaml risk.*）：
  当日亏损 = 实时权益 − day_start_equity ≤ −max_daily_loss_pct × day_start_equity → 禁止开新仓，按 R10 撤入场类挂单，
           事件与 webhook daily_loss_halt；次日自动解除（day_start 重新记录）。
  回撤 = 1 − 实时权益 / peak_equity ≥ max_drawdown_pct → 写 KILL 文件（settings.KILL_PATH），事件与 webhook drawdown_kill；
       只能人工删除 KILL 解除。
  balance / equity_marks 任一读取失败 → 拒绝开仓，事件 guard_data_unavailable。不得按“无亏损”处理。
!! 本模块不下单，只读取与写本地状态；撤单由 desk/trigger_review.py 执行。
"""
from __future__ import annotations

import json
import math

from desk import settings

MARKS = settings.STATE / "equity_marks.json"
RISK_DEFAULTS = {"max_daily_loss_pct": 0.025, "max_drawdown_pct": 0.10}


class GuardDataUnavailable(RuntimeError):
    pass


def risk_cfg(cfg) -> dict:
    return {**RISK_DEFAULTS, **(cfg.get("risk") or {})}


def _read_marks() -> dict:
    if not MARKS.exists():
        return {}
    try:
        m = json.loads(MARKS.read_text(encoding="utf-8"))
    except Exception as e:   # 文件损坏：拒绝开仓，不得当作首次运行
        raise GuardDataUnavailable(f"equity_marks 读取失败 {type(e).__name__}")
    if not isinstance(m, dict):
        raise GuardDataUnavailable("equity_marks 格式错误")
    return m


def update_marks(live: float, today: str | None = None) -> dict:
    """记录 day_start_equity（北京时间每日首次读取）与 peak_equity。live 必须是有效正数。"""
    if live is None or not isinstance(live, (int, float)) or not math.isfinite(live) or live <= 0:
        raise GuardDataUnavailable(f"实时权益无效：{live!r}")
    today = today or settings.today()
    m = _read_marks()
    if m.get("day") != today or not m.get("day_start_equity"):
        m.update(day=today, day_start_equity=float(live), day_start_time=settings.now_str())
    m["peak_equity"] = max(float(m.get("peak_equity") or 0), float(live))
    m["last_equity"] = float(live); m["last_time"] = settings.now_str()
    settings.write_json(MARKS, m)
    return m


def evaluate(cfg, live: float, today: str | None = None) -> dict:
    """纯判定 + 更新标记。返回 {live, day_start, daily_pnl, daily_halt, peak, drawdown, drawdown_kill}。数据无效抛 GuardDataUnavailable。"""
    rc = risk_cfg(cfg)
    m = update_marks(live, today)
    ds, pk = float(m["day_start_equity"]), float(m["peak_equity"])
    pnl = live - ds
    dd = 1 - live / pk if pk > 0 else 0.0
    return {"live": live, "day_start": ds, "daily_pnl": pnl, "daily_halt": pnl <= -float(rc["max_daily_loss_pct"]) * ds + 1e-9,
            "peak": pk, "drawdown": dd, "drawdown_kill": dd >= float(rc["max_drawdown_pct"]) - 1e-12, "day": m["day"]}


def _notify(kind: str, message: str, notifier=None):
    try:
        if notifier is None:
            from desk.demo_ws import notify as notifier
        notifier({"kind": kind, "message": message})
    except Exception as e:
        print(f"[equity_guard] 通知 {kind} 失败 {type(e).__name__}", flush=True)


def apply_breach(cfg, st: dict, notifier=None) -> list[str]:
    """越限动作：回撤 → 写 KILL + drawdown_kill；当日亏损 → daily_loss_halt（每日一次）。返回触发的事件名。"""
    from desk import okx_demo as X
    out = []
    if st.get("drawdown_kill") and not settings.KILL_PATH.exists():
        settings.KILL_PATH.parent.mkdir(parents=True, exist_ok=True)
        settings.KILL_PATH.write_text(f"drawdown_kill {settings.now_str()} drawdown={st['drawdown']:.4f} "
                                      f"live={st['live']:.2f} peak={st['peak']:.2f}\n", encoding="utf-8")
        X.log_event("drawdown_kill", drawdown=round(st["drawdown"], 6), live=st["live"], peak=st["peak"], kill_path=str(settings.KILL_PATH))
        _notify("drawdown_kill", f"回撤 {st['drawdown']:.2%} 达到上限，已写 KILL（{settings.KILL_PATH}），需人工删除解除", notifier)
        out.append("drawdown_kill")
    if st.get("daily_halt"):
        m = _read_marks()
        if m.get("daily_halt_day") != st["day"]:
            m["daily_halt_day"] = st["day"]; settings.write_json(MARKS, m)
            X.log_event("daily_loss_halt", daily_pnl=round(st["daily_pnl"], 4), day_start=st["day_start"], live=st["live"])
            _notify("daily_loss_halt", f"当日权益亏损 {st['daily_pnl']:.2f}（日初 {st['day_start']:.2f}）达到上限，今日停开新仓", notifier)
            out.append("daily_loss_halt")
    return out


def status(cfg, live: float | None = None) -> dict:
    """读取实时权益并判定；任何失败返回 {ok: False, unavailable: True, error}（调用方必须拒绝开仓）。"""
    from desk import okx_demo as X
    try:
        if live is None:
            from desk import demo_exec as DX
            live = DX.usdt_equity()
        st = evaluate(cfg, float(live) if live is not None else None)
        st["ok"] = not (st["daily_halt"] or st["drawdown_kill"])
        st["unavailable"] = False
        return st
    except Exception as e:
        X.log_event("guard_data_unavailable", error=f"{type(e).__name__}: {e}"[:300])
        return {"ok": False, "unavailable": True, "error": f"{type(e).__name__}: {e}"}


def reasons(cfg, live: float | None = None) -> list[str]:
    """供 check_open / 限价补单护栏使用：返回拒绝原因（空=通过）。会触发越限动作（写 KILL、发通知）。"""
    st = status(cfg, live)
    if st.get("unavailable"):
        return [f"[GUARD_DATA] 风控数据不可用（{st['error'][:120]}），拒绝开仓（guard_data_unavailable）"]
    apply_breach(cfg, st)
    rc = risk_cfg(cfg)
    out = []
    if st["daily_halt"]:
        out.append(f"[DAILY_LOSS] 当日权益亏损 {st['daily_pnl']:.2f} ≤ −{float(rc['max_daily_loss_pct']):.2%}×日初 {st['day_start']:.2f}，今日禁止开仓")
    if st["drawdown_kill"]:
        out.append(f"[DRAWDOWN] 回撤 {st['drawdown']:.2%} ≥ {float(rc['max_drawdown_pct']):.0%}，已写 KILL")
    return out
