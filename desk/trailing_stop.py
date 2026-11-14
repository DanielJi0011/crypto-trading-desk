"""移动止损（2026-11-14 用户批准）：趋势市非衰竭仓位以移动止损替代固定止盈。

背景与对照证据：开台 25 天的对照账显示，单边趋势里固定止盈切得太勤、漏收益（如 2026-11-11 BTC 空单
在 72,429 止盈离场后价格又跌到 67,683，一笔漏掉约 +1,771 USDT）；而原始止损 25 天里只被触及 1 次。
用户批准的改法（不是取消退出规则）：本场 LLM 行情判定生效（方向与订单方向一致、强度 strong/moderate）
且阶段不是 late_exhaustion 时，新开仓位【不挂固定止盈单】，改用移动止损退出；late_exhaustion / 非趋势 /
判定失败保持现状（固定止盈）。TM 的计划止盈照常给出，下单校验（RR≥1.5 等全部硬护栏）仍按计划止盈
计算，一项不改；计划止盈的新用途 = 移动止损的激活线基准。

机制（代码侧棘轮，不用 OKX 原生 move_order_stop，理由见设计说明
goals/ta-desk-okx-simulated-trading-operation/hidden_files/trailing-stop-design-2026-11-14.md）：
demo_watch 每轮（5 分钟）对登记为 exit_mode="trailing" 的持仓做一次棘轮计算——
  激活前：止损就是原始止损，纹丝不动；激活线 = 开仓价向有利方向走出「计划止盈距离 ×
  execution.trailing_stop.activation_frac_of_tp（默认 0.5）」；
  激活后：止损候选 = 历史最有利价 ∓ trail_atr_mult（默认 1.0）× 开仓时 ATR14（日线口径，存登记表
  atr14_open），只向有利方向移动、绝不放松，经 demo_exec.move_stop（amend 优先、失败走
  tighten_by_replace 先挂新再撤旧）落到交易所那张既有止损单上；
  移动止损触发平仓后与止损/止盈事件同路：demo_watch / demo_ws 检测到平仓算法单时，若该仓位登记为
  trailing，则以 reason="trailing_stop" 唤醒再讨论（desk/rediscuss.py 有专门简报）。
HALT 全冻结期间不做任何棘轮动作（调用方不调、本模块内再守一道）。不追溯既有持仓：只有新决策在
tm_order_tool.validate 里被标记者才进入本机制（登记表 exit_mode 字段是唯一事实来源）。

状态：全部存在 state/ledger/demo_orders.json 的 positions[inst] 登记里（exit_mode / plan_tp /
atr14_open / trail{activated,best_px,stop_px}），重启不丢；止损事件区分用的暂存放在 watch.trailing_exits。
!! 只经 desk/okx_demo.py 访问模拟盘；除经 demo_exec.move_stop 收紧止损外不下单、不改单。
"""
from __future__ import annotations

from desk import okx_demo as X
from desk import settings

EXIT_MODE = "trailing"
STASH_WINDOW_MS = 15 * 60 * 1000   # 止损事件 uTime 与暂存时刻的允许偏差（暂存是 watch 发现持仓消失时写的，略晚于成交）


def tcfg(cfg) -> dict:
    from desk import demo_exec as DX
    return DX.ecfg(cfg)["trailing_stop"]


def enabled(cfg) -> bool:
    return bool(tcfg(cfg).get("enabled"))


# ------------------------------------------------------------------ 纯计算（一轮棘轮）
def activation_px(side: str, entry: float, plan_tp: float, frac: float) -> float:
    """激活线：从开仓价向计划止盈方向走出 frac × 计划止盈距离。（plan_tp-entry）带符号，多空通用。"""
    return entry + (float(plan_tp) - float(entry)) * float(frac)


def step(side: str, entry: float, plan_tp: float, atr: float, cur_stop: float,
         best_px, last: float, tc: dict, tick: float = 0.0) -> dict:
    """一轮棘轮计算（无副作用）。best_px=迄今最有利价（多头取高、空头取低，None=未记录）。
    返回 {"activated": bool, "best_px": float, "new_stop": float|None, "act_px": float}。
    规则：未越过激活线 → 永不移动；越线后候选 = best ∓ trail_atr_mult×ATR；候选必须比现止损更紧
    且收紧幅度 > max(tick, 0.05×ATR)（防每轮为微小差额频繁改单）才给出 new_stop。绝不放松。"""
    sg = 1 if side == "long" else -1
    best = float(last) if best_px is None else (max(float(best_px), float(last)) if sg > 0 else min(float(best_px), float(last)))
    act = activation_px(side, entry, plan_tp, float(tc.get("activation_frac_of_tp", 0.5)))
    activated = best >= act if sg > 0 else best <= act
    new_stop = None
    if activated and atr > 0:
        cand = best - sg * float(tc.get("trail_atr_mult", 1.0)) * float(atr)
        improve = (cand - float(cur_stop)) if sg > 0 else (float(cur_stop) - cand)
        if improve > max(float(tick or 0), 0.05 * float(atr)):
            new_stop = cand
    return {"activated": activated, "best_px": best, "new_stop": new_stop, "act_px": act}


# ------------------------------------------------------------------ 每轮执行（demo_watch 调用）
def _tick(inst) -> float:
    try:
        return float(X.instrument(inst)["tickSz"])
    except Exception:
        return 0.0


def check(cfg, positions=None, halt: bool = False, move=None, tick_of=None) -> list:
    """一轮移动止损棘轮。positions 缺省取模拟盘 book 的非旧纸面持仓（book 字段：
    inst_id/side/avg_px/sl_px/price/contracts/pos_side）。move 缺省 demo_exec.move_stop（可注入测试）。
    返回给调用方打印的日志行。开关关 / HALT → 空列表、零动作。"""
    if halt or not enabled(cfg):
        return []
    tc = tcfg(cfg)
    from desk import demo_exec as DX
    if positions is None:
        positions = [p for p in DX.book(cfg)["positions"] if not p.get("legacy")]
    r = DX.reg()
    metas = r.get("positions") or {}
    lines, changed = [], False
    for p in positions:
        inst = p["inst_id"]
        m = metas.get(inst)
        if not m or m.get("exit_mode") != EXIT_MODE:
            continue
        atr = float(m.get("atr14_open") or 0)
        plan_tp, entry = m.get("plan_tp"), float(p.get("avg_px") or 0)
        last, cur = float(p.get("price") or 0), p.get("sl_px")
        if not (atr > 0 and plan_tp and entry > 0 and last > 0 and cur):
            X.log_event("trailing_skip", inst_id=inst, why="数据不全", atr14_open=m.get("atr14_open"),
                        plan_tp=plan_tp, avg_px=p.get("avg_px"), price=p.get("price"), sl_px=cur)
            continue
        st = m.setdefault("trail", {"activated": False, "best_px": None, "stop_px": cur})
        was_act, old_best = bool(st.get("activated")), st.get("best_px")
        res = step(p["side"], entry, float(plan_tp), atr, float(cur), old_best, last, tc,
                   tick=(tick_of or _tick)(inst))
        if res["best_px"] != old_best:
            st["best_px"] = res["best_px"]; changed = True
        if res["activated"] and not was_act:
            st["activated"] = True; changed = True
            X.log_event("trailing_activated", inst_id=inst, decision_id=m.get("decision_id"), entry=entry,
                        act_px=res["act_px"], best_px=res["best_px"], plan_tp=float(plan_tp), atr14=atr)
            lines.append(f"{inst} 移动止损已激活：最有利价 {res['best_px']:g} 越过激活线 {res['act_px']:g}"
                         f"（计划止盈距离×{float(tc.get('activation_frac_of_tp', 0.5)):g}），跟踪距离 "
                         f"{float(tc.get('trail_atr_mult', 1.0)):g}×ATR14={atr:g}")
        if res["new_stop"] is not None:
            ok, msg = (move or DX.move_stop)(cfg, p, res["new_stop"])
            if ok:
                st["stop_px"] = res["new_stop"]; changed = True
                X.log_event("trailing_stop_moved", inst_id=inst, decision_id=m.get("decision_id"),
                            old=float(cur), new_sl=res["new_stop"], best_px=res["best_px"], atr14=atr)
                lines.append(f"{inst} 移动止损跟进：止损 {float(cur):g} → {res['new_stop']:g}（最有利价 {res['best_px']:g}）")
            else:
                X.log_event("trailing_stop_move_failed", inst_id=inst, new_sl=res["new_stop"], msg=str(msg)[:200])
                lines.append(f"{inst} 移动止损跟进失败：{msg}")
    if changed:
        DX.save_reg(r)
    return lines


# ------------------------------------------------------------------ 止损事件区分（再讨论 reason）
def position_is_trailing(inst) -> bool:
    """该币当前持仓登记是否为移动止损仓位（demo_ws 实时检测路径用：此时登记通常还在）。"""
    from desk import demo_exec as DX
    try:
        return (DX.reg().get("positions") or {}).get(inst, {}).get("exit_mode") == EXIT_MODE
    except Exception:
        return False


def note_exit(reg_d: dict, inst, meta, at_ms: int) -> bool:
    """持仓登记被清理前调用：移动止损仓位的退出标记暂存进 reg['watch']['trailing_exits']，
    供随后（同轮或 WS 之外）的止损事件检测把 reason 区分成 trailing_stop。返回是否暂存。"""
    if (meta or {}).get("exit_mode") != EXIT_MODE:
        return False
    reg_d.setdefault("watch", {}).setdefault("trailing_exits", {})[inst] = {
        "decision_id": meta.get("decision_id"), "at_ms": int(at_ms)}
    return True


def consume_exit_reason(reg_d: dict, inst, event_ms) -> str | None:
    """止损事件（uTime=event_ms）命中暂存且时间窗内 → 返回 "trailing_stop" 并消费暂存；否则 None。
    时间窗防的是：同一币之后另一张固定止盈仓位的止损被误标成移动止损。"""
    w = reg_d.get("watch") or {}
    stash = (w.get("trailing_exits") or {}).get(inst)
    if not stash:
        return None
    if event_ms and int(event_ms) > int(stash.get("at_ms") or 0) + STASH_WINDOW_MS:
        return None
    w["trailing_exits"].pop(inst, None)
    return "trailing_stop"


def prune_stash(reg_d: dict, floor_ms: int):
    """清理过期暂存（与 demo_watch 的 handled_algos 同一保留窗口调用）。"""
    w = reg_d.get("watch") or {}
    if w.get("trailing_exits"):
        w["trailing_exits"] = {k: v for k, v in w["trailing_exits"].items()
                               if int(v.get("at_ms") or 0) >= floor_ms}
