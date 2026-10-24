"""任务B：BTC 全员再讨论（复用 desk.rediscuss 同一流程），只采集决策、不自动下单；
- 模拟盘账本视图剔除旧纸面仓（BTC 旧纸面仓不在模拟盘）
- 向全部角色注入模拟盘现状
- 拦截 demo_exec 的下单/撤单与 ledger/pending 本地写入；决策写入 state/ledger/btc_rerun_decision.json 供人工护栏复核后执行。"""
import json, sys, os, logging
sys.path.insert(0, "/workspace/ta-desk")
from desk import settings, analysis, ledger, manager, okx_data, pending, roles, scoring, stances, demo_exec as DX, okx_demo as X
from desk import okx_demo as _evsrc; _evsrc.set_event_source("ops")  # O3：运维脚本事件来源，不计入每日下单数
from desk import llm as dllm
logging.basicConfig(level=logging.WARNING)
cfg = settings.load_config(); ledger.assert_paper(cfg)
coin = next(c for c in cfg["coins"] if c["inst_id"] == "BTC-USDT-SWAP")
assert dllm.prepare_env(cfg), "LLM key 缺失"
roles.ensure_playbooks(); tracker = dllm.UsageTracker(cfg); X.startup(cfg)
_book = DX.book
def book_demo_only(cfg, prices=None):
    b = _book(cfg, prices); b["positions"] = [p for p in b["positions"] if not p.get("legacy")]; return b
DX.book = book_demo_only
bk = DX.book(cfg)
trig = [f"{t['instId']} {t['side']}/{t.get('posSide')} {t['sz']}张 触发价 {t['triggerPx']} 止损/止盈 " +
        ",".join(f"{a.get('slTriggerPx')}/{a.get('tpTriggerPx')}" for a in (t.get('attachAlgoOrds') or [])) for t in bk["triggers"]]
posl = [f"{p['inst_id']} {p['side']} {p['leverage']:g}x {p['contracts']}张 均价 {p['avg_px']} 止损 {p['sl_px']} 止盈 {p['tp_px']}" for p in bk["positions"]]
CTX = ("## OKX 模拟盘账户现状（执行以此为准）\n权益基准 101898.76 USDT。持仓：" + ("；".join(posl) or "无") +
       "。条件挂单：" + ("；".join(trig) or "无") + "。BTC 在模拟盘上没有任何持仓或挂单（本地纸面账本里的 BTC 旧仓已作废，不算现有持仓）。")
print(CTX, flush=True)
_sb = roles.set_briefs
def sb(b):
    b = dict(b)
    for r in list(roles.ROLE_KEYS) + ["trade_manager"]:
        b[r] = (CTX + "\n\n" + b.get(r, "")).strip()
    _sb(b)
roles.set_briefs = sb
CAP = {}
def cap(name):
    def f(*a, **k):
        CAP.setdefault(name, []).append({"args": [x for x in a if not isinstance(x, dict) or "positions" not in x and "coins" not in x], "kw": k})
        if name in ("open_now", "place_trigger"):
            return False, {"reasons": ["DRY-RUN：由人工护栏复核后执行"], "checks": {}}
        return []
    return f
for n in ("open_now", "place_trigger", "cancel_triggers", "close", "reduce", "move_stop"):
    setattr(DX, n, cap(n))
for n in ("open_position", "close_position", "move_stop"):
    setattr(ledger, n, cap("ledger." + n))
for n in ("place", "cancel"):
    setattr(pending, n, cap("pending." + n))
date = settings.today()
res = analysis.analyze(cfg, coin, date, bk, tracker)
fs = res["final_state"]; fs["_signal"] = res["signal"]
st = stances.extract(fs, cfg, tracker)
rec = manager.decide_and_execute(cfg, coin, date, fs, st, tracker, source="manual_rerun:demo")
rec["report_dir"] = res["report_dir"]
scoring.record_stances(cfg, rec)
out = {"rec": rec, "captured": CAP, "usage": tracker.summary(), "ctx": CTX}
settings.write_json(settings.STATE / "ledger" / "btc_rerun_decision.json", json.loads(json.dumps(out, default=str)))
print("DONE", rec["decision_id"], rec["final_action"], rec["consensus"], json.dumps(rec["aggregate"], default=str, ensure_ascii=False))
