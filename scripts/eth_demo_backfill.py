"""任务A：ETH 模拟盘补单（仅 OKX 模拟盘，复用 desk.okx_demo）。"""
import json, math, sys, uuid
sys.path.insert(0, "/workspace/ta-desk")
from desk import okx_demo as X
from desk import okx_demo as _evsrc; _evsrc.set_event_source("ops")  # O3：运维脚本事件来源，不计入每日下单数
INST="ETH-USDT-SWAP"; EQ=101898.76; PCT=13.156; SL=2495.455; TP=2306.02
assert X.verify(), "模拟盘自检失败"
i=X.instrument(INST); ct,lot,mn=float(i["ctVal"]),float(i["lotSz"]),float(i["minSz"])
last=float(X.get("/api/v5/market/ticker",instId=INST)["data"][0]["last"])
print("ctVal",ct,"lotSz",lot,"tickSz",i["tickSz"],"last",last)
if last>=SL or last<=TP: print("现价越过止损/止盈，不下单"); sys.exit(0)
margin=EQ*PCT/100; n=math.floor(margin/last/ct/lot+1e-9)*lot; sz=X._dec(n)
print("margin",round(margin,2),"sz",sz)
if "--go" not in sys.argv: sys.exit(0)
X.post("/api/v5/account/set-leverage",{"instId":INST,"lever":"1","mgnMode":"isolated","posSide":"short"})
att={"attachAlgoClOrdId":"ta"+uuid.uuid4().hex[:20],"slTriggerPx":X._px(INST,SL),"slOrdPx":"-1","slTriggerPxType":"last","tpTriggerPx":X._px(INST,TP),"tpOrdPx":"-1","tpTriggerPxType":"last"}
body={"instId":INST,"tdMode":"isolated","side":"sell","posSide":"short","ordType":"market","sz":sz,"clOrdId":"ta"+uuid.uuid4().hex[:20],"attachAlgoOrds":[att]}
try:
    d=X.post("/api/v5/trade/order",body)
except X.DemoError as e: print("ERR",e); sys.exit(1)
print(json.dumps(d["data"],ensure_ascii=False))
oid=d["data"][0]["ordId"]
import time; time.sleep(2)
o=X.get("/api/v5/trade/order",instId=INST,ordId=oid)["data"][0]
print("order",{k:o.get(k) for k in ("ordId","state","side","posSide","sz","accFillSz","avgPx","lever","attachAlgoOrds")})
for t in ("conditional","oco"):
    for a in X.get("/api/v5/trade/orders-algo-pending",ordType=t,instType="SWAP")["data"]:
        print("algo",{k:a.get(k) for k in ("algoId","instId","ordType","posSide","sz","slTriggerPx","tpTriggerPx","state")})
X.log_event("eth_backfill_ok",inst_id=INST,ordId=oid,sz=sz,sl=att["slTriggerPx"],tp=att["tpTriggerPx"])
