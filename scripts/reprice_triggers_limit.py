"""把旧的「市价触发」(orderPx=-1) 条件入场单撤掉并按原参数重挂为「触发后限价」(execution.entry_limit)。

!! 只用 OKX 模拟盘（desk.okx_demo：OKX_DEMO_* + x-simulated-trading: 1），禁止实盘。
- 张数/触发价/止损止盈/杠杆/有效期全部沿用旧单（张数、触发价、杠杆取交易所实际挂单；止损止盈、有效期、decision_id 等取登记表），不按权益重算。
- 撤单前先过 demo_exec.check_open 护栏（与 place_trigger 相同，按旧张数），不通过则不撤。
- 受保护单（protected_algo_ids）只有在 --allow-protected 明确列出时才替换，替换后把 config.yaml 里的旧 algoId 换成新的。
- 撤单后限价重挂失败 → 立即按原参数以 orderPx=-1（市价触发）挂回，并记录原因。
用法：.venv/bin/python scripts/reprice_triggers_limit.py ALGO_ID [ALGO_ID ...] [--allow-protected ALGO_ID ...] [--execute]
不带 --execute 只打印计划。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from desk import demo_exec as DX, okx_demo as X, settings  # noqa: E402
from desk import okx_demo as _evsrc; _evsrc.set_event_source("ops")  # O3：运维脚本事件来源，不计入每日下单数

CONFIG = settings.CODE / "config.yaml"


def _close(a, b, tick):
    return abs(float(a) - float(b)) <= float(tick) / 2 + 1e-12


def plan(cfg, aid):
    live = {t["algoId"]: t for t in DX.trigger_orders()}
    t = live.get(aid)
    if not t:
        raise SystemExit(f"{aid} 不在挂单中")
    inst = t["instId"]
    m = DX.reg()["triggers"].get(aid)
    if not m:
        raise SystemExit(f"{aid} 不在本地登记表 demo_orders.json")
    tick = X.instrument(inst)["tickSz"]
    att = (t.get("attachAlgoOrds") or [{}])[0]
    side = "long" if t["side"] == "buy" else "short"
    problems, notes = [], []
    if side != m["side"]:
        problems.append(f"方向不一致：交易所 {side} / 登记表 {m['side']}")
    if not _close(t["triggerPx"], m["trigger_px"], tick):
        problems.append(f"触发价不一致：交易所 {t['triggerPx']} / 登记表 {m['trigger_px']}")
    for k, ak in (("stop_loss", "slTriggerPx"), ("take_profit", "tpTriggerPx")):
        if m.get(k) and att.get(ak) and not _close(att[ak], X._px(inst, m[k]), tick):
            problems.append(f"{k} 不一致：交易所 {att.get(ak)} / 登记表 {m[k]}")
        if bool(m.get(k)) != bool(att.get(ak)):
            problems.append(f"{k} 有无不一致：交易所 {att.get(ak)!r} / 登记表 {m.get(k)!r}")
    if str(m.get("sz")) != str(t["sz"]):
        notes.append(f"登记表张数 {m.get('sz')} 与交易所实际 {t['sz']} 不一致 → 按交易所实际张数 {t['sz']} 重挂")
    if float(m.get("leverage") or 1) != float(t.get("lever") or 1):
        notes.append(f"登记表杠杆 {m.get('leverage')} 与交易所 {t.get('lever')} 不一致 → 用交易所 {t.get('lever')}")
    o = {**{k: m.get(k) for k in DX.TRIGGER_META_KEYS}, "side": side, "trigger_px": float(t["triggerPx"]),
         "leverage": float(t.get("lever") or m.get("leverage") or 1)}
    return {"inst": inst, "old": t, "meta": m, "o": o, "sz": t["sz"], "expires": m.get("expires"),
            "ord_px_old": t.get("ordPx"), "problems": problems, "notes": notes}


def guard(cfg, p):
    bk = DX.book(cfg)
    bk2 = {**bk, "triggers": [t for t in bk["triggers"] if t["instId"] != p["inst"]]}
    qty = float(p["sz"]) * float(X.instrument(p["inst"])["ctVal"])
    reasons, _ = DX.check_open(cfg, bk2, p["inst"], qty, p["o"]["trigger_px"], p["o"].get("stop_loss"), p["o"]["side"], p["o"]["leverage"])
    return reasons


def update_protected(old, new):
    s = CONFIG.read_text()
    if old not in s:
        raise RuntimeError(f"config.yaml 里找不到 {old}")
    line_re = re.compile(r'^(\s*protected_algo_ids:.*)$', re.M)
    mline = line_re.search(s)
    if not mline or old not in mline.group(1):
        raise RuntimeError("protected_algo_ids 行中没有旧 algoId")
    ln = mline.group(1).replace(old, new)
    ln = re.sub(r'#.*$', f"# 用户确认保留的条件单（SOL 空 97.75 张 @108.6，{settings.now_str()[:16]} 由 {old} 改挂为触发后限价单 {new}），"
                         "不被新决策替换/撤销/过期撤单", ln)
    CONFIG.write_text(s[:mline.start(1)] + ln + s[mline.end(1):])


def run(cfg, aid, allow_protected, execute):
    p = plan(cfg, aid)
    prot = {str(x) for x in DX.ecfg(cfg)["protected_algo_ids"]}
    is_prot = aid in prot
    print(json.dumps({k: p[k] for k in ("inst", "sz", "expires", "ord_px_old", "problems", "notes")} | {"o": p["o"], "protected": is_prot},
                     ensure_ascii=False, default=str))
    if p["ord_px_old"] not in ("-1", -1, None, ""):
        print(f"{aid} 已是限价 orderPx={p['ord_px_old']}，跳过"); return None
    if p["problems"]:
        print(f"{aid} 登记表与交易所不一致，不处理：{p['problems']}"); return None
    if is_prot and aid not in allow_protected:
        print(f"{aid} 受保护且未在 --allow-protected 中，不处理"); return None
    reasons = guard(cfg, p)
    if reasons:
        print(f"{aid} 护栏不通过，不撤单：{reasons}"); X.log_event("reprice_rejected", algoId=aid, reasons=reasons); return None
    if not execute:
        print("（计划模式，未执行）"); return None
    state = DX.cancel_trigger(p["inst"], aid, "改挂为触发后限价（reprice_triggers_limit）")
    if state != "canceled":
        print(f"!! {aid} 撤单后状态 {state}（可能已触发），不重挂"); return None
    extra = {"replaced_from": aid, "replaced_at": settings.now_str(), "orig_created": p["meta"].get("created"),
             "orig_algoClOrdId": p["meta"].get("algoClOrdId"),
             "note": (p["meta"].get("note") or "") + f"；{settings.now_str()} 由 {aid}（orderPx=-1）改挂为触发后限价" +
                     ("；" + "；".join(p["notes"]) if p["notes"] else "")}
    fallback_reason = None
    try:
        meta = DX.submit_trigger(cfg, p["inst"], p["o"], p["sz"], p["expires"], extra=extra)
    except Exception as e:
        fallback_reason = f"限价重挂失败：{type(e).__name__}: {e}"
        print("!! " + fallback_reason + " → 按原参数市价触发挂回")
        meta = DX.submit_trigger(cfg, p["inst"], p["o"], p["sz"], p["expires"], force_market=True,
                                 extra={**extra, "fallback_reason": fallback_reason})
    X.log_event("reprice_done", old_algoId=aid, new_algoId=meta["algoId"], inst_id=p["inst"], order_px=meta["order_px"],
                fallback_reason=fallback_reason)
    if is_prot:
        update_protected(aid, meta["algoId"])
        print(f"config.yaml protected_algo_ids: {aid} → {meta['algoId']}")
    print(f"{aid} → {meta['algoId']} orderPx={meta['order_px']}" + (f"（{fallback_reason}）" if fallback_reason else ""))
    return meta["algoId"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("algo_ids", nargs="+")
    ap.add_argument("--allow-protected", nargs="*", default=[])
    ap.add_argument("--execute", action="store_true")
    a = ap.parse_args()
    cfg = settings.load_config()
    if not DX.is_demo(cfg):
        raise SystemExit("execution.venue 不是 okx_demo，退出")
    if not DX.ecfg(cfg)["entry_limit"].get("enabled"):
        raise SystemExit("entry_limit.enabled=false，退出")
    for aid in a.algo_ids:
        run(settings.load_config(), str(aid), set(a.allow_protected), a.execute)


if __name__ == "__main__":
    main()
