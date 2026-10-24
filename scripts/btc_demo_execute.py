"""任务B执行：读取 btc_rerun_decision.json 的聚合决策，人工护栏复核后用 demo_exec.place_trigger 在模拟盘挂条件单。"""
import json, sys, time
sys.path.insert(0, "/workspace/ta-desk")
from desk import settings, demo_exec as DX, okx_demo as X
from desk import okx_demo as _evsrc; _evsrc.set_event_source("ops")  # O3：运维脚本事件来源，不计入每日下单数
cfg = settings.load_config()
o = json.load(open(settings.STATE/"ledger"/"btc_rerun_decision.json"))["captured"]["place_trigger"][0]["args"][1]
b = DX.book(cfg); b["positions"] = [p for p in b["positions"] if not p.get("legacy")]
inst="BTC-USDT-SWAP"; EQ=101898.76; i=X.instrument(inst)
sz=float(X.contracts(inst, o["size_pct"]*EQ/100*o["leverage"]/o["trigger_px"])); notional=sz*float(i["ctVal"])*o["trigger_px"]
trig_n=sum(float(t["sz"])*float(X.instrument(t["instId"])["ctVal"])*float(t["triggerPx"]) for t in b["triggers"])
tot=b["exposure"]+trig_n+notional
insts={p["inst_id"] for p in b["positions"]}|{t["instId"] for t in b["triggers"]}
chk={"margin":notional/o["leverage"],"margin_cap":EQ*0.2,"total_notional":tot,"cap":EQ*1.8,"n_after":len(insts|{inst}),
     "btc_exists":inst in insts,"lev":o["leverage"],"sl":o["stop_loss"],"last":float(X.get("/api/v5/market/ticker",instId=inst)["data"][0]["last"])}
print(json.dumps(chk))
ok = chk["margin"]<=chk["margin_cap"] and tot<=chk["cap"] and chk["n_after"]<=3 and not chk["btc_exists"] and o["leverage"]<=5 and o["stop_loss"]>o["trigger_px"]
if not ok or "--go" not in sys.argv: print("not executing", ok); sys.exit(0)
try: r = DX.place_trigger(cfg, b, inst, o)
except X.DemoError as e: print("ERR", e); sys.exit(1)
print(json.dumps(r, ensure_ascii=False, default=str)); time.sleep(2)
for a in X.get("/api/v5/trade/orders-algo-pending", ordType="trigger", instType="SWAP")["data"]:
    print({k:a.get(k) for k in ("algoId","instId","side","posSide","sz","lever","triggerPx","state","attachAlgoOrds")})
