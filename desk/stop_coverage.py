"""R11 止损覆盖检查（demo_watch 每轮调用）。

每个模拟盘持仓（非旧纸面仓）都必须有 reduceOnly 平仓方向、带止损触发价、张数合计覆盖持仓张数的算法单。
1. 覆盖正常：记住该仓最近一次的止损/止盈价（state/stop_coverage.json 的 last_known），供缺失时重挂。
2. 缺失或张数不足：连续 confirm_rounds 轮确认后发 stop_coverage_missing 告警（同一缺口只告警一次，恢复后清除）。
3. execution.stop_coverage_auto_replace=true 且未 HALT 时自动重挂：
   全无止损 → 按 last_known 止损（没有则按 risk.stop_max_pct 从现价算）走 demo_exec.tighten_by_replace（先挂新，确认生效）；
   有止损但张数不足 → 按原止损止盈和持仓总张数先挂新再撤旧（_replace_close_algos）。
全部在每币执行锁内完成；任何读取失败只记事件，不下单。
"""
from __future__ import annotations

import json

from desk import okx_demo as X
from desk import settings

PATH = settings.STATE / "stop_coverage.json"
CONFIRM_ROUNDS = 2


def _load() -> dict:
    try:
        return json.loads(PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"last_known": {}, "missing": {}}


def _save(d: dict):
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def _src(source):
    return {"source": source} if source else {}


def _key(p) -> str:
    return f"{p['inst_id']}:{p.get('pos_side') or 'net'}"


def coverage(p, algos) -> dict:
    close_side = "sell" if p["side"] == "long" else "buy"
    mine = [a for a in algos if a.get("instId", p["inst_id"]) == p["inst_id"] and a.get("posSide", "net") == (p.get("pos_side") or "net")
            and a.get("side") in (None, "", close_side) and a.get("slTriggerPx")]
    sz = sum(float(a.get("sz") or 0) for a in mine)
    sls = [float(a["slTriggerPx"]) for a in mine]
    tps = [float(a["tpTriggerPx"]) for a in mine if a.get("tpTriggerPx")]
    sl = (max(sls) if p["side"] == "long" else min(sls)) if sls else None
    return {"algos": mine, "covered_sz": sz, "ok": bool(mine) and sz >= float(p["contracts"]) - 1e-9, "sl": sl,
            "tp": tps[0] if tps else None}


def check(cfg, bk=None, halt=False, only=None, source=None) -> list:
    """only：只检查这些合约（演练用，隔离其他仓）；source：事件来源标记（演练传 test）。"""
    from desk import demo_exec as DX, exec_lock, sizing
    out, st = [], _load()
    auto = bool(DX.ecfg(cfg).get("stop_coverage_auto_replace", False))
    bk = bk or DX.book(cfg)
    seen = set()
    for p in bk["positions"]:
        if p.get("legacy") or p.get("venue") != "okx_demo" or (only and p["inst_id"] not in only):
            continue
        k = _key(p); seen.add(k)
        try:
            cv = coverage(p, DX.close_algos(p["inst_id"]))
        except Exception as e:
            X.log_event("stop_coverage_error", **_src(source), inst_id=p["inst_id"], error=type(e).__name__); continue
        if cv["ok"]:
            st["last_known"][k] = {"sl": cv["sl"], "tp": cv["tp"], "time": settings.now_str()}
            if st["missing"].pop(k, None):
                X.log_event("stop_coverage_restored", **_src(source), inst_id=p["inst_id"], pos_side=p.get("pos_side"), sl=cv["sl"])
            out.append((k, "ok")); continue
        m = st["missing"].setdefault(k, {"rounds": 0, "alerted": False, "since": settings.now_str()})
        m["rounds"] += 1
        if m["rounds"] < CONFIRM_ROUNDS:
            out.append((k, "pending_confirm")); continue
        if not m["alerted"]:
            DX._alert("stop_coverage_missing", **_src(source), inst_id=p["inst_id"], pos_side=p.get("pos_side"), contracts=p["contracts"],
                      covered_sz=cv["covered_sz"], auto_replace=auto and not halt,
                      message=f"{p['inst_id']} {p.get('pos_side')} 持仓 {p['contracts']} 张，止损只覆盖 {cv['covered_sz']:g} 张")
            m["alerted"] = True
        if not auto or halt:
            out.append((k, "missing")); continue
        lk = st["last_known"].get(k) or {}
        try:
            with exec_lock.hold(p["inst_id"], why="stop_coverage"):
                if cv["algos"]:
                    nid = DX._replace_close_algos(p, cv["sl"], cv["tp"] or lk.get("tp"), X._dec(p["contracts"]))
                    ok = bool(nid)
                else:
                    smax = float(sizing.rcfg(cfg)["stop_max_pct"])
                    px = float(p["price"])
                    sl = lk.get("sl") or (px * (1 - smax) if p["side"] == "long" else px * (1 + smax))
                    ok, nid = DX.tighten_by_replace(cfg, p, float(sl), tp=lk.get("tp"))
            X.log_event("stop_coverage_replaced" if ok else "stop_coverage_replace_failed", **_src(source), inst_id=p["inst_id"],
                        pos_side=p.get("pos_side"), result=str(nid)[:200])
            out.append((k, "replaced" if ok else "replace_failed"))
        except Exception as e:
            DX._alert("stop_replace_failed", inst_id=p["inst_id"], step="coverage", error=f"{type(e).__name__}: {e}"[:300])
            out.append((k, "replace_failed"))
    for k in list(st["missing"]):
        if k not in seen and not only:
            st["missing"].pop(k)
    _save(st)
    return out
