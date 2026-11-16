"""持有期到期处理（由 desk/demo_watch.py 到期分支调用）。

到期路径只有两条（mode，见 expiry_extend 配置）：
- full_discussion（默认）：对该币跑一次与早晚报相同的全员讨论（desk/expiry_discuss.py，后台子进程），
  结论由 demo_watch 应用：持有/调整 → apply_extend 延期；平仓/失败/超时 → 到期平仓；
  达 max_extensions → 不讨论直接平仓。HALT 由调用方处理（不平仓、不讨论）。
- off（enabled=false 或 mode=off）：旧行为——到期直接平仓，不延期。
本模块只提供 mode/ecfg/due/apply_extend 等共享件与日报行渲染，不调用任何下单接口；
平仓由调用方经 demo_exec.close 执行。

历史：2026-10-29 起曾有 simple 模式（交易经理简化问答 judge/_call_tm），2026-10-30 全员讨论
成为默认后 simple 再未被配置启用，2026-11-15 代码审查后退休删除；mode 读到 simple 等未知值
一律回退 full_discussion。report_lines 仍渲染历史 simple 事件（无 mode 字段的 expiry_* 事件）。

决策 4（2026-11-15 用户批准）：exit_mode=trailing 的持仓到期时，若该币最新存档行情判定（代码门后的
生效阶段）仍为与持仓同向、强度 strong/moderate、非 late_exhaustion 的趋势，demo_watch 在走上面的
讨论之前先自动续一期（trailing_extend_check，本文件末段）；判定转弱/衰竭/反向/失败则原路径
一行不改。续期计入 max_extensions 总上限，每次到期重新核对最新判定，没有永久通行证。"""
from __future__ import annotations

import datetime as _dt

from desk import settings

FMT = "%Y-%m-%d %H:%M:%S%z"
DEFAULTS = {"enabled": False, "mode": "full_discussion", "max_extensions": 2, "pre_window_minutes": 5,
            "discussion_timeout_minutes": 45}
MODES = ("full_discussion", "off")


def mode(cfg) -> str:
    """off=旧行为到期直接平仓；full_discussion=全员讨论（默认，desk/expiry_discuss.py）。enabled=false 等同 off；
    simple 已退休，未知值回退 full_discussion。"""
    c = ecfg(cfg)
    if not c["enabled"]:
        return "off"
    m = str(c.get("mode") or "full_discussion").strip().lower()
    return m if m in MODES else "full_discussion"


def ecfg(cfg) -> dict:
    d = dict(DEFAULTS)
    d.update((cfg or {}).get("expiry_extend") or {})
    return d


def due(cfg, m: dict, now) -> bool:
    """到期或到期前一个检查周期内（pre_window_minutes）。"""
    if not m.get("deadline"):
        return False
    dl = _dt.datetime.strptime(m["deadline"], FMT)
    win = _dt.timedelta(minutes=float(ecfg(cfg)["pre_window_minutes"])) if ecfg(cfg)["enabled"] else _dt.timedelta(0)
    return now >= dl - win


def latest_decision(inst: str) -> dict | None:
    from desk import manager
    recs = [r for r in settings.read_jsonl(manager.DECISIONS) if r.get("inst_id") == inst]
    return recs[-1] if recs else None


def consensus_agrees(cfg, side: str, S) -> bool:
    if S is None:
        return False
    th = float((cfg.get("manager") or {}).get("consensus_threshold", 0.1))
    return (float(S) > th) if side == "long" else (float(S) < -th)


def apply_extend(m: dict, v: dict):
    """延期写回登记：更新 deadline，记次数/累计天数/原 deadline。止损止盈不动。"""
    m.setdefault("orig_deadline", m.get("deadline"))
    m["extensions"] = int(m.get("extensions") or 0) + 1
    m["extended_days"] = float(m.get("extended_days") or 0) + float(v["days"])
    m["deadline"] = v["new_deadline"]
    m["last_extension"] = {k: v.get(k) for k in ("deadline", "new_deadline", "days", "reason", "time")}
    m.pop("expiry_attempts", None)


# ------------------------------------------------------------------ 决策 4：移动止损仓位到期自动续期
TRAILING_HORIZON_DEFAULTS = {"enabled": False}


def trailing_horizon_cfg(cfg) -> dict:
    """execution.trailing_horizon_extend（代码默认关闭、config 打开，与 execution.trailing_stop 同款开关模式）。"""
    d = dict(TRAILING_HORIZON_DEFAULTS)
    d.update(((cfg or {}).get("execution") or {}).get("trailing_horizon_extend") or {})
    return d


def latest_regime_call(inst: str) -> dict | None:
    """该币最新一条存档行情判定（state/ledger/regime_calls.jsonl，按追加顺序取最后一条匹配 inst_id 的）。
    存档里的 judge 是 build_regime 过代码门（apply_stage_gate）后的判定：stage 为生效值，模型原始值在 stage_raw。"""
    from desk import regime_judge
    recs = [r for r in settings.read_jsonl(regime_judge.REGIME_CALLS) if r.get("inst_id") == inst]
    return recs[-1] if recs else None


def trailing_extend_check(cfg, inst: str, m: dict, side: str | None) -> dict | None:
    """决策 4：exit_mode=trailing 的持仓到期时，最新存档判定仍为同向趋势（强度 strong/moderate、
    生效阶段 early/mid，即非 late_exhaustion）→ 自动续一期（horizon_days），不触发到期复核。
    任一条件不满足返回 None，调用方走原到期路径：
      开关关（本项开关或 expiry_extend 总开关）/ 非 trailing 仓位 / 无方向或无 deadline /
      已达 max_extensions 总上限（与原路径共用 extensions 计数）/ 本期全员讨论已在进行（不抢）/
      无存档判定、最新一场判定失败（judge 为空）/ 判定反向、无趋势或生效阶段为 late_exhaustion。
    本函数不写登记、不发事件：返回 {"days", "new_deadline", "reason", "verdict"}，由调用方
    apply_extend 落盘并记 horizon_extended_trailing 事件（verdict 即事件里引用的那条判定）。"""
    if not trailing_horizon_cfg(cfg).get("enabled") or not ecfg(cfg)["enabled"]:
        return None
    if m.get("exit_mode") != "trailing":
        return None
    side = side or m.get("side")
    if side not in ("long", "short") or not m.get("deadline"):
        return None
    if int(m.get("extensions") or 0) >= int(ecfg(cfg)["max_extensions"]):
        return None                       # 总延期上限（config 注释：所有模式共享）：交给原路径按上限处理
    if m.get("expiry_discussed_for") == m.get("deadline"):
        return None                       # 本期全员讨论已启动：不抢，让讨论结论决定
    rec = latest_regime_call(inst)
    j = (rec or {}).get("judge")
    if not rec or rec.get("failed") or not j:
        return None
    if j.get("direction") != side or j.get("strength") not in ("strong", "moderate") \
            or j.get("stage") not in ("early", "mid"):
        return None
    days = float(m.get("horizon_days") or 5) or 5.0
    nd = (_dt.datetime.strptime(m["deadline"], FMT) + _dt.timedelta(days=days)).strftime(FMT)
    verdict = {"direction": j.get("direction"), "strength": j.get("strength"), "stage": j.get("stage"),
               "stage_raw": j.get("stage_raw"), "stage_overridden": bool(j.get("stage_overridden")),
               "time": rec.get("time"), "date": rec.get("date"), "session": rec.get("session"),
               "decision_id": rec.get("decision_id"), "judge_standard": rec.get("judge_standard")}
    reason = (f"移动止损仓位到期自动续期：最新判定（{rec.get('date')} {rec.get('session')}场，决策 {rec.get('decision_id')}）"
              f"仍为{'空头' if side == 'short' else '多头'}趋势（{j.get('direction')}/{j.get('strength')}/{j.get('stage')}），"
              f"按决策 4 续一期 {days:g} 天")
    return {"days": days, "new_deadline": nd, "reason": reason, "verdict": verdict}


def report_lines(date: str) -> list[str]:
    """日报/晚报：持有期到期：延期 X 天/平仓，原因。"""
    from desk import okx_demo as X
    out = []
    for e in settings.read_jsonl(X.EVENTS):
        if not str(e.get("time", "")).startswith(date):
            continue
        if e.get("event") == "horizon_extended_trailing":
            vd = e.get("verdict") or {}
            out.append(f"- 持有期到期：{e.get('inst_id')} 移动止损仓位自动续期 {float(e.get('days') or 0):g} 天"
                       f"（新到期 {e.get('new_deadline')}）：最新判定仍为同向趋势"
                       f"（{vd.get('direction')}/{vd.get('strength')}/{vd.get('stage')}，{vd.get('date')} {vd.get('session')}场，决策 {vd.get('decision_id')}）")
        elif e.get("event") == "expiry_extended" and e.get("mode") == "full_discussion":
            out.append(f"- 持有期到期：{e.get('inst_id')} 全员讨论结论{'持有' if e.get('outcome') == 'held' else '调整后持有'}"
                       f"（{e.get('final_action')}，共识 S={_fs(e.get('consensus'))}，决策 {e.get('decision_id')}），延期 {float(e.get('days') or 0):g} 天（新到期 {e.get('new_deadline')}）")
        elif e.get("event") == "expiry_closed" and e.get("mode") == "full_discussion":
            out.append(f"- 持有期到期：{e.get('inst_id')} 平仓，全员讨论{'结论平仓' if e.get('consensus') is not None else '未得出结论'}"
                       f"（共识 S={_fs(e.get('consensus'))}），原因：{e.get('reason')}")
        elif e.get("event") == "expiry_extended":
            out.append(f"- 持有期到期：{e.get('inst_id')} 延期 {float(e.get('days') or 0):g} 天（新到期 {e.get('new_deadline')}），原因：{e.get('reason')}")
        elif e.get("event") == "expiry_closed":
            out.append(f"- 持有期到期：{e.get('inst_id')} 平仓，原因：{e.get('reason')}")
    return out


def _fs(v):
    try:
        return f"{float(v):+.3f}"
    except (TypeError, ValueError):
        return "—"
