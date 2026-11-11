"""【已停用，仅 execution.venue=paper 时使用】本地纸面盘中监控：止损/止盈盯市 + 本地条件单触发 + 止损后再讨论。
用户 2026-10-20 要求彻底停掉 run_monitor.sh，不要再启动。模拟盘直连流程的止损检测/再讨论见 desk/demo_watch.py
（schedule_rediscuss 仍在本文件，供 demo_watch 调用）。

用法：python -m desk.monitor --once        # 运行一次（cron / run_monitor.sh 用）
      python -m desk.monitor --loop 300     # 常驻循环，每 300 秒一次
无挂单且无持仓时直接退出：不请求行情、不调用 LLM。"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import subprocess
import sys
import time
import traceback

from pydantic import BaseModel, Field

from desk import settings

REDISCUSS = settings.STATE / "ledger" / "rediscuss.json"


def log(msg):
    print(f"[monitor {settings.now_str()}] {msg}", flush=True)


def PCFG(cfg) -> dict:
    d = {"mode": "auto", "bar": "1m", "mark_bar": "5m", "near_pct": 0.3, "default_expire_hours": 24,
         "immediate_band_pct": 0.3, "rediscuss_after_stop": True, "rediscuss_cooldown_hours": 6}
    return {**d, **(cfg.get("pending") or {})}


# ---------------- 可替换（测试注入）
def MIN_BARS(inst, bar):
    from desk import okx_data
    return okx_data.candles(inst, bar, 300)


def LAST(inst):
    from desk import okx_data
    return okx_data.last_price(inst)


class EntryConfirm(BaseModel):
    confirm: bool = Field(description="true=按条件单开仓；false=跳过（撤销该条件单）")
    stop_loss: float | None = Field(default=None, description="如需调整止损可给出新止损价（多单低于、空单高于成交价），否则省略")
    reason: str = Field(description="中文理由（≤80字）")


def CONFIRM(cfg, coin, o, ctx) -> EntryConfirm:
    """confirm 模式：触发/接近触发时，用最新行情再问一次交易经理（一次轻量调用）。"""
    from desk import llm as dllm
    from desk.manager import market_ctx
    dllm.prepare_env(cfg)
    tracker = dllm.UsageTracker(cfg)
    mk = market_ctx(coin["inst_id"])
    prompt = "\n".join([
        f"你是交易经理。{coin['name']}（OKX 永续 {coin['inst_id']}）的{'做多' if o.get('side', 'long') == 'long' else '做空'}条件入场单{'已触发' if ctx['hit'] else '价格已接近触发价'}，请确认是否开仓。",
        f"条件单：{'价格≥' if o['condition'] == 'gte' else '价格≤'} {o['trigger_px']:.6g} 时开{'多' if o.get('side', 'long') == 'long' else '空'}（{o.get('leverage', 1)}x），止损 {o['stop_loss']:.6g}，"
        f"止盈 {o.get('take_profit') or '无'}，仓位 {o['size_pct']:.1f}%；创建于 {o['created']}（共识 S={o.get('consensus')}）；理由：{o.get('reason', '')}",
        f"最新行情：现价 {mk['last']:.6g}，参考成交价 {ctx['px']:.6g}；5日 {mk['ret_5d']:+.2f}%，20日 {mk['ret_20d']:+.2f}%，ATR14 {mk['atr14']:.6g}；"
        f"20日高 {mk['hi_20']:.6g} 低 {mk['lo_20']:.6g}",
        f"最近 30 根 {ctx['bar']} K线收盘：{ctx['closes']}",
        "如果触发是假突破/急跌未止、或原入场理由已失效，请 confirm=false。理由用中文。"])
    tok = dllm.STAGE.set(f"{coin['inst_id']}/条件单确认")
    try:
        r = dllm.make_llm(cfg, "quick", tracker, role="trade_manager").with_structured_output(EntryConfirm).invoke(prompt)
        r = EntryConfirm(**r) if isinstance(r, dict) else r
    finally:
        dllm.STAGE.reset(tok)
        settings.append_jsonl(settings.STATE / "usage" / "monitor.jsonl", {"time": settings.now_str(), **tracker.summary()})
    return r


def SPAWN(args: list[str], logfile):
    with open(logfile, "a") as f:
        subprocess.Popen([sys.executable, "-m", "desk.rediscuss", *args], cwd=str(settings.CODE), stdout=f,
                         stderr=subprocess.STDOUT, start_new_session=True, env=os.environ.copy())


# ---------------- 止损后再讨论（后台、每个止损事件至多一次 + 冷却）
def schedule_rediscuss(cfg, trade: dict) -> str:
    """止损/止盈→再讨论（trade["reason"] 为 "sl" 或 "tp"，缺省 "sl"）；reason="rr_recheck" 为开仓后 RR 复核
    （desk/rr_recheck.py，trade_id='DEMO-<posId>'，可带 trade["detail"] 事实包随事件存档；豁免跨事件冷却，见下）。
    demo_watch（轮询）与 demo_ws（WebSocket）共用：trade_id 统一为 'DEMO-<algoId>'，
    已完成记录、在途记录与 6h 冷却都在 rediscuss.json；用文件锁保证跨进程同一笔止损只触发一次。
    标记语义（2026-11-09）：启动只记在途，子进程决策落盘后由 complete_rediscuss 移入已完成；在途超时（REDISCUSS_STALE_HOURS）视为进程已死可续跑。"""
    import fcntl
    REDISCUSS.parent.mkdir(parents=True, exist_ok=True)
    with open(str(REDISCUSS) + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            return _schedule_rediscuss_locked(cfg, trade)
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


REDISCUSS_STALE_HOURS = 2.0   # 在途超过此时长未完成 → 视为子进程已死，可续跑


def complete_rediscuss(inst: str, trade_id: str) -> bool:
    """再讨论子进程在决策落盘后调用（desk/rediscuss.py）：把事件从在途移到已完成。
    旧流程在启动时就标记已处理，子进程被杀会把这次再讨论永久吞掉；2026-11-09 改为
    启动只记在途（inflight），完成落盘后才进 events，在途超时未完成可被重新启动续跑。"""
    import fcntl
    REDISCUSS.parent.mkdir(parents=True, exist_ok=True)
    with open(str(REDISCUSS) + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            st = settings.read_json(REDISCUSS, {})
            s = st.setdefault(inst, {"events": [], "last": None})
            inflight = s.setdefault("inflight", {})
            if trade_id not in inflight:
                return False
            del inflight[trade_id]
            if trade_id not in s["events"]:
                s["events"].append(trade_id)
            if s.get("details"):
                s["details"].pop(trade_id, None)   # rr_recheck 的事实包随完成一并清理
            settings.write_json(REDISCUSS, st)
            return True
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def _schedule_rediscuss_locked(cfg, trade: dict) -> str:
    from desk import okx_demo, pending
    P = PCFG(cfg)
    inst = trade["inst_id"]
    if not P["rediscuss_after_stop"]:
        return "关闭"
    st = settings.read_json(REDISCUSS, {})
    s = st.setdefault(inst, {"events": [], "last": None})
    inflight = s.setdefault("inflight", {})
    if trade["trade_id"] in s["events"]:
        return "该事件已讨论过"
    now = settings.now()
    reason = trade.get("reason") if trade.get("reason") in ("tp", "rr_recheck") else "sl"

    def _spawn() -> str:
        prev_last = s.get("last")
        s["last"] = settings.now_str()
        inflight[trade["trade_id"]] = s["last"]
        if trade.get("detail"):
            s.setdefault("details", {})[trade["trade_id"]] = trade["detail"]   # rr_recheck：事实包随事件存档，rediscuss 子进程读取
        settings.write_json(REDISCUSS, st)
        settings.LOGS.mkdir(parents=True, exist_ok=True)
        try:
            SPAWN(["--coin", inst.split("-")[0], "--event", trade["trade_id"], "--reason", reason],
                  settings.LOGS / f"rediscuss-{settings.today()}.log")
        except Exception as e:   # 启动失败不留假在途、也不起冷却，下个巡检周期可重试
            inflight.pop(trade["trade_id"], None)
            if s.get("details"):
                s["details"].pop(trade["trade_id"], None)
            s["last"] = prev_last
            settings.write_json(REDISCUSS, st)
            okx_demo.log_event("rediscuss_spawn_failed", inst_id=inst, trade_id=trade["trade_id"], reason=str(e)[:200])
            return "启动失败"
        pending.event("再讨论启动", inst, trade_id=trade["trade_id"], reason=reason)
        okx_demo.log_event("rediscuss_started", inst_id=inst, trade_id=trade["trade_id"], reason=reason)
        return "已启动"

    started = inflight.get(trade["trade_id"])
    if started:
        try:
            age = now - _dt.datetime.strptime(started, "%Y-%m-%d %H:%M:%S%z")
        except (TypeError, ValueError):
            age = None
        if age is not None and age < _dt.timedelta(hours=REDISCUSS_STALE_HOURS):
            return "再讨论进行中"
        # 同一事件的续跑不受冷却约束：冷却只拦新事件，续跑被冷却挡住等于又把这次讨论吞掉
        okx_demo.log_event("rediscuss_resume", inst_id=inst, trade_id=trade["trade_id"], reason="在途超时未完成，续跑")
        return _spawn()
    # rr_recheck 豁免跨事件冷却：冷却是为止损后防 LLM 连环触发设的；RR 复核是活仓位几何异常的安全复核，
    # 且每仓至多一次（rr_recheck.json 的 pkey 状态 + 此处 events 去重双重保证），被冷却吞掉等于没补上这个复核。
    if reason != "rr_recheck" and s["last"] and now - _dt.datetime.strptime(s["last"], "%Y-%m-%d %H:%M:%S%z") < _dt.timedelta(hours=float(P["rediscuss_cooldown_hours"])):
        s["events"].append(trade["trade_id"])   # 冷却跳过与旧行为一致：记为已处理，不再重试
        settings.write_json(REDISCUSS, st)
        pending.event("再讨论跳过", inst, trade_id=trade["trade_id"], reason=f"冷却期 {P['rediscuss_cooldown_hours']}h 内", trigger=trade.get("reason", "sl"))
        okx_demo.log_event("rediscuss_skipped", inst_id=inst, trade_id=trade["trade_id"], reason=f"冷却期 {P['rediscuss_cooldown_hours']}h 内")
        return "冷却中"
    return _spawn()


# ---------------- 条件单处理
def process_pending(cfg, now=None) -> list[dict]:
    from desk import ledger, pending
    P = PCFG(cfg)
    now = now or settings.now()
    out = []
    coins = {c["inst_id"]: c for c in cfg["coins"]}
    for inst, o in list(pending.load().items()):
        if pending.expired(o, now):
            pending.cancel(cfg, inst, "过期"); out.append({"inst": inst, "result": "过期"}); continue
        bars = MIN_BARS(inst, P["bar"])
        near = float(P["near_pct"]) if P["mode"] == "confirm" else 0.0
        hit = pending.find_trigger(o, bars, near) if len(bars) else None
        if len(bars):
            p = pending.load()
            if inst in p:
                step = int(bars.ts.diff().median()) if len(bars) > 1 else 60_000
                p[inst]["last_check_ts_ms"] = int(bars.ts.iloc[-1]) + step
                pending.save(p)
        if not hit:
            continue
        bts, px, real = hit
        pending.event("触发" if real else "接近触发", inst, id=o["id"], px=px, bar_ts=bts)
        # 护栏（开仓前）：紧急停止、已有持仓不加仓、共识 S、止损必填
        reasons = []
        if settings.no_new_positions():
            reasons.append("KILL 紧急停止文件存在")
        if any(t["inst_id"] == inst for t in ledger.open_trades()):
            reasons.append("已有持仓，不加仓")
        side = o.get("side", "long")
        th = float(cfg["manager"].get("consensus_threshold", 0.1))
        S = float(o.get("consensus") or 0)
        if (side == "long" and S <= th) or (side == "short" and S >= -th):
            reasons.append(f"共识 S={S:+.3f} 不支持{'做多' if side == 'long' else '做空'}")
        sl = o.get("stop_loss")
        if not sl or (side == "long" and float(sl) >= px) or (side == "short" and float(sl) <= px):
            reasons.append("没有有效止损（多单止损须低于、空单须高于成交价）")
        if reasons:
            pending.cancel(cfg, inst, "护栏拒绝：" + "；".join(reasons))
            out.append({"inst": inst, "result": "拒单", "reasons": reasons}); continue
        if P["mode"] == "confirm":
            try:
                c = CONFIRM(cfg, coins[inst], o, {"hit": real, "px": px, "bar": P["bar"],
                                                  "closes": [round(x, 6) for x in bars.Close.tail(30)]})
            except Exception as e:
                pending.event("确认失败", inst, id=o["id"], error=f"{type(e).__name__}: {e}")
                out.append({"inst": inst, "result": "确认失败"}); continue   # 保留挂单，下次再试
            if not c.confirm:
                pending.cancel(cfg, inst, "交易经理确认跳过：" + c.reason)
                out.append({"inst": inst, "result": "跳过", "reason": c.reason}); continue
            if c.stop_loss and ((side == "long" and float(c.stop_loss) < px) or (side == "short" and float(c.stop_loss) > px)):
                sl = float(c.stop_loss)
            if not real:
                px = LAST(inst)
            pending.event("确认买入", inst, id=o["id"], reason=c.reason)
        bk = ledger.book(cfg)
        lev = float(o.get("leverage") or 1)
        ok, info = ledger.open_position(cfg, inst, float(o["size_pct"]) * bk["equity"] / 100 * lev, float(sl), o.get("take_profit"),
                                        int(o.get("horizon_days") or 5), o.get("decision_id") or o["id"],
                                        f"条件单{o['id']}：{o.get('reason', '')}", bk["equity"], ref_px=px,
                                        side=side, leverage=lev)
        p = pending.load(); p.pop(inst, None); pending.save(p)
        if ok:
            pending.event("成交", inst, id=o["id"], trade_id=info["trade_id"], px=info["px"], qty=info["qty"], side=side,
                          leverage=lev, margin=info["margin"], liq_px=info["liq_px"])
            out.append({"inst": inst, "result": "成交", **info})
        else:
            pending.event("拒单", inst, id=o["id"], reasons=info["reasons"])
            out.append({"inst": inst, "result": "拒单", "reasons": info["reasons"]})
    return out


def run_once(cfg) -> dict:
    from desk import ledger, okx_data, pending
    ledger.assert_paper(cfg)
    okx_data._cache.clear()
    pend, opens = pending.load(), ledger.open_trades()
    if not pend and not opens:
        log("无挂单、无持仓：跳过（不请求行情、不调用 LLM）")
        return {"idle": True}
    P = PCFG(cfg)
    closed = []
    if opens:
        orig = ledger.BARS
        ledger.BARS = lambda inst: MIN_BARS(inst, P["mark_bar"])
        try:
            closed = ledger.mark_to_market(cfg)
        finally:
            ledger.BARS = orig
        for t in closed:
            log(f"平仓 {t['trade_id']} {t['inst_id']} {t['exit_reason']} @ {t['exit_px']} 盈亏 {float(t['pnl_usd']):+.2f}")
            pending.event("盯市平仓", t["inst_id"], trade_id=t["trade_id"], reason=t["exit_reason"], px=t["exit_px"])
            if t["exit_reason"] == "止损":
                log(f"止损后再讨论：{schedule_rediscuss(cfg, t)}")
    res = process_pending(cfg)
    for r in res:
        log(f"条件单 {r['inst']}：{r['result']} {r.get('reasons') or r.get('reason') or ''}")
    log(f"完成：持仓 {len(ledger.open_trades())}，挂单 {len(pending.load())}")
    return {"closed": closed, "pending": res}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", type=int, default=0, help="常驻循环间隔秒数")
    a = ap.parse_args(argv)
    while True:
        try:
            run_once(settings.load_config())
        except Exception as e:
            traceback.print_exc(); log(f"错误：{type(e).__name__}: {e}")
            if a.once or not a.loop:
                return 1
        if a.once or not a.loop:
            return 0
        time.sleep(a.loop)


if __name__ == "__main__":
    sys.exit(main())
