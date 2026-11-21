"""保本锁（2026-11-20 用户批准）：固定止盈核心仓的浮盈达到 1R 后，盯盘脚本把止损移到入场价。

背景与对照证据：设计文档 goals/ta-desk-okx-simulated-trading-operation/hidden_files/
breakeven-lock-design-2026-11-20.md。本台 18 笔历史回测里，1R / 止盈距离 0.5 / 0.7 三档一笔出场都没
改变（输家早死、赢家长跑）；0.3 档被数据否决（净 −276）。保本锁的价值不在期望增益，而在给「已赚
不少、未到止盈」的固定止盈仓位封一个下限——这类仓位此前毫无下限保护（移动止损仓有棘轮、衰竭仓有
半仓尺寸控制）。量化文献的大样本一致显示保本锁期望小负到中性、早触发明确亏钱，故触发取 1R（晚档）。

机制（代码侧、脚本驱动，与移动止损同一套价格口径，不用 LLM 判断）：
demo_watch 每轮（5 分钟）对登记了 breakeven 元数据的持仓检查一次——
  资格（开仓时冻结，唯一事实来源 = 持仓登记的 breakeven 字段，由 tm_order_tool.validate §3.6b 在
  新入场 open / 无持仓 pending 时判定）：固定止盈（非移动止损）核心仓、开仓时未处末段衰竭降级、
  非条件加仓腿；R = |计划入场价 − 原始止损| 在下单时冻结写入（entry_ref / orig_stop / r_dist），
  严禁日后用收紧过的现止损重算（设计文档 4.3 的 BTC 反例：按现止损算会在开仓当天误触发）；
  触发：现价自实际成交均价（book avg_px）向有利方向走出 ≥ trigger_r（默认 1.0）× r_dist；
  动作：经 demo_exec.move_stop 把交易所那张既有止损单改到入场价正点位（avg_px）。只进不退——
  现止损已比入场价更紧（TM 已手动收过）则不动，只记 locked（satisfied）；move_stop 本身也只许收紧；
  改单失败记 breakeven_lock_failed 事件并在 watch 日志行告警，不标 locked、下轮重试，绝不静默。
与移动止损互斥：exit_mode="trailing" 的仓位不适用（棘轮第一步即近保本位，是保本锁的超集）；
末段衰竭仓不适用（2026-11-18 SOL 教训：衰竭段止损需要结构缓冲，与硬拉入场价正面对撞，且半仓
已控尾部风险）；条件加仓腿不适用（与移动止损范围先例一致）。
保本出场（回碰入场价止损成交）：平仓原因独立记 breakeven_lock（日报记账为「保本锁」），像移动
止损出场一样唤醒再讨论；不记止损冷却/披露账本（它不是止损，是一次中性退出），不计入止损笔数。
HALT 全冻结期间不执行上锁（调用方不调、本模块内再守一道）；已上锁的止损本来就挂在交易所，不受影响。
!! 只经 desk/okx_demo.py 访问模拟盘；除经 demo_exec.move_stop 收紧止损外不下单、不改单。
"""
from __future__ import annotations

from desk import okx_demo as X
from desk import settings

STASH_WINDOW_MS = 15 * 60 * 1000   # 与 trailing 同窗：止损事件 uTime 与暂存时刻的允许偏差
EXIT_PX_TOL = 0.0025               # 保本出场价与锁定价（入场价）的相对偏差容忍（滑点）；超出视为别的止损


def bcfg(cfg) -> dict:
    from desk import demo_exec as DX
    return DX.ecfg(cfg)["breakeven_lock"]


def enabled(cfg) -> bool:
    return bool(bcfg(cfg).get("enabled"))


# ------------------------------------------------------------------ 纯计算
def trigger_px(side: str, entry: float, r_dist: float, trigger_r: float) -> float:
    """触发线：从实际入场价向有利方向走出 trigger_r × R（R=原始止损距离，冻结值）。多空通用。"""
    sg = 1 if side == "long" else -1
    return float(entry) + sg * float(trigger_r) * float(r_dist)


def step(side: str, entry: float, r_dist: float, cur_stop: float, last: float, bc: dict) -> dict:
    """一轮保本锁判定（无副作用）。返回 {"triggered": bool, "trig_px": float, "lock_px": float,
    "need_move": bool}。lock_px = 入场价正点位；need_move = 锁定价比现止损更紧（现止损已更紧时
    triggered 仍成立但 need_move=False，调用方只记 locked、不改单）。绝不放松。"""
    trig = trigger_px(side, entry, r_dist, float(bc.get("trigger_r", 1.0)))
    if side == "long":
        return {"triggered": float(last) >= trig - 1e-9, "trig_px": trig,
                "lock_px": float(entry), "need_move": float(entry) > float(cur_stop)}
    return {"triggered": float(last) <= trig + 1e-9, "trig_px": trig,
            "lock_px": float(entry), "need_move": float(entry) < float(cur_stop)}


# ------------------------------------------------------------------ 每轮执行（demo_watch 调用）
def check(cfg, positions=None, halt: bool = False, move=None) -> list:
    """一轮保本锁检查。positions 缺省取模拟盘 book 的非旧纸面持仓（字段同 trailing_stop.check）。
    move 缺省 demo_exec.move_stop（可注入测试）。返回给调用方打印的日志行。
    开关关 / HALT → 空列表、零动作。"""
    if halt or not enabled(cfg):
        return []
    bc = bcfg(cfg)
    from desk import demo_exec as DX
    if positions is None:
        positions = [p for p in DX.book(cfg)["positions"] if not p.get("legacy")]
    r = DX.reg()
    metas = r.get("positions") or {}
    lines, changed = [], False
    for p in positions:
        inst = p["inst_id"]
        m = metas.get(inst)
        if not m or m.get("exit_mode") == "trailing":
            continue   # 移动止损仓互斥（validate 阶段本就不会给它 breakeven 元数据，此处再守一道）
        b = m.get("breakeven") or {}
        if not b.get("eligible") or b.get("locked"):
            continue
        entry, last, cur = float(p.get("avg_px") or 0), float(p.get("price") or 0), p.get("sl_px")
        r_dist = float(b.get("r_dist") or 0)
        if not (entry > 0 and last > 0 and cur and r_dist > 0):
            X.log_event("breakeven_skip", inst_id=inst, why="数据不全", r_dist=b.get("r_dist"),
                        avg_px=p.get("avg_px"), price=p.get("price"), sl_px=cur)
            continue
        res = step(p["side"], entry, r_dist, float(cur), last, bc)
        if not res["triggered"]:
            continue
        if not res["need_move"]:
            b["locked"] = True; b["lock_px"] = res["lock_px"]; b["locked_at"] = settings.now_str()
            b["already_tighter"] = True; changed = True
            X.log_event("breakeven_locked", inst_id=inst, decision_id=m.get("decision_id"),
                        trigger_px=res["trig_px"], at_px=last, old_stop=float(cur), new_stop=float(cur),
                        lock_px=res["lock_px"], r_dist=r_dist, already_tighter=True,
                        time=settings.now_str())
            lines.append(f"{inst} 保本锁触发（现价 {last:g} 越过 1R 线 {res['trig_px']:g}）："
                         f"现止损 {float(cur):g} 已紧于入场价 {res['lock_px']:g}，不改单，标记已锁定")
            continue
        ok, msg = (move or DX.move_stop)(cfg, p, res["lock_px"])
        if ok:
            b["locked"] = True; b["lock_px"] = res["lock_px"]; b["locked_at"] = settings.now_str()
            changed = True
            X.log_event("breakeven_locked", inst_id=inst, decision_id=m.get("decision_id"),
                        trigger_px=res["trig_px"], at_px=last, old_stop=float(cur),
                        new_stop=res["lock_px"], lock_px=res["lock_px"], r_dist=r_dist,
                        time=settings.now_str())
            lines.append(f"{inst} 保本锁上锁：止损 {float(cur):g} → {res['lock_px']:g}（入场价；"
                         f"现价 {last:g} 已越过 1R 触发线 {res['trig_px']:g}，R={r_dist:g}）")
        else:
            X.log_event("breakeven_lock_failed", inst_id=inst, decision_id=m.get("decision_id"),
                        lock_px=res["lock_px"], old_stop=float(cur), msg=str(msg)[:200])
            lines.append(f"{inst} 保本锁上锁失败（告警，下轮重试）：{msg}")
    if changed:
        DX.save_reg(r)
    return lines


# ------------------------------------------------------------------ 止损事件区分（再讨论 reason）
def position_is_locked(inst) -> bool:
    """该币当前持仓登记是否已上保本锁（demo_ws 实时检测路径用：此时登记通常还在）。"""
    from desk import demo_exec as DX
    try:
        return bool(((DX.reg().get("positions") or {}).get(inst, {}).get("breakeven") or {}).get("locked"))
    except Exception:
        return False


def lock_px_of(inst):
    """该币当前登记的保本锁定价（入场价）；未上锁/无登记 → None。"""
    from desk import demo_exec as DX
    try:
        return ((DX.reg().get("positions") or {}).get(inst, {}).get("breakeven") or {}).get("lock_px")
    except Exception:
        return None


def px_matches_lock(exit_px, lock_px) -> bool:
    """出场价是否≈锁定价（容忍滑点）；任一缺失时不否决（只凭登记/暂存标记判定）。"""
    if not exit_px or not lock_px:
        return True
    return abs(float(exit_px) - float(lock_px)) / float(lock_px) <= EXIT_PX_TOL


def note_exit(reg_d: dict, inst, meta, at_ms: int) -> bool:
    """持仓登记被清理前调用：已上锁仓位的退出标记暂存进 reg['watch']['breakeven_exits']，
    供随后（同轮或 WS 之外）的止损事件检测把 reason 区分成 breakeven_lock。返回是否暂存。"""
    if not ((meta or {}).get("breakeven") or {}).get("locked"):
        return False
    reg_d.setdefault("watch", {}).setdefault("breakeven_exits", {})[inst] = {
        "decision_id": meta.get("decision_id"),
        "lock_px": (meta.get("breakeven") or {}).get("lock_px"), "at_ms": int(at_ms)}
    return True


def consume_exit_reason(reg_d: dict, inst, event_ms, exit_px=None) -> str | None:
    """止损事件命中暂存、时间窗内、且出场价与锁定价（入场价）偏差在容忍内 → 返回 "breakeven_lock"
    并消费暂存；否则 None。出场价校验防的是：上锁后 TM 又把止损收得更紧、随后触及那个更紧的
    止损——那是普通止损，不许误标成保本出场。exit_px 缺失时只凭暂存+时间窗判定。"""
    w = reg_d.get("watch") or {}
    stash = (w.get("breakeven_exits") or {}).get(inst)
    if not stash:
        return None
    if event_ms and int(event_ms) > int(stash.get("at_ms") or 0) + STASH_WINDOW_MS:
        return None
    lock = stash.get("lock_px")
    if exit_px and lock and abs(float(exit_px) - float(lock)) / float(lock) > EXIT_PX_TOL:
        return None
    w["breakeven_exits"].pop(inst, None)
    return "breakeven_lock"


def match_exit(reg_d: dict, inst, event_ms, exit_px=None) -> bool:
    """止损事件是否保本出场：登记还在 → locked 标记 + 出场价≈锁定价；登记已清 → 暂存标记同口径
    （consume_exit_reason）。出场价校验防上锁后 TM 再收紧的止损被误标（见 consume_exit_reason）。"""
    if position_is_locked(inst):
        return px_matches_lock(exit_px, lock_px_of(inst))
    return consume_exit_reason(reg_d, inst, event_ms, exit_px) == "breakeven_lock"


def prune_stash(reg_d: dict, floor_ms: int):
    """清理过期暂存（与 demo_watch 的 handled_algos 同一保留窗口调用）。"""
    w = reg_d.get("watch") or {}
    if w.get("breakeven_exits"):
        w["breakeven_exits"] = {k: v for k, v in w["breakeven_exits"].items()
                                if int(v.get("at_ms") or 0) >= floor_ms}
