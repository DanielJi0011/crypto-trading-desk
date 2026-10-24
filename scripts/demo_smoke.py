"""OKX 模拟盘冒烟测试（禁止实盘：全部经 desk/okx_demo.py，OKX_DEMO_* + x-simulated-trading: 1）。
1) 最小张数、远离现价的 trigger 条件单（附止损止盈）→ 查到后撤掉；2) 最小张数市价开 → 查附带止损 → 立即平；3) 查无残留。
不写纸面账本、不写 demo_orders.json；不会动其他已有挂单（只撤本脚本自己的单）。
用法：set -a; . state/secrets.env; set +a; .venv/bin/python scripts/demo_smoke.py [INST]"""
import sys, time, uuid
sys.path.insert(0, ".")
from desk import demo_exec as DX, okx_demo as X, okx_data
from desk import okx_demo as _evsrc; _evsrc.set_event_source("test")  # O3：运维脚本事件来源，不计入每日下单数

inst = sys.argv[1] if len(sys.argv) > 1 else "SOL-USDT-SWAP"
assert X.verify(), "模拟环境自检失败"
i = X.instrument(inst); mn = i["minSz"]; px = okx_data.last_price(inst)
ps = DX._ps("long"); DX._set_lev(inst, 1, ps)
before = {t["algoId"] for t in DX.trigger_orders()}
# 1) 远离现价的条件单（现价 ×0.5 才做多触发，不会触发）
cl = "tt" + uuid.uuid4().hex[:20]
body = {"instId": inst, "tdMode": "isolated", "side": "buy", "ordType": "trigger", "sz": mn, "triggerPx": X._px(inst, px * 0.5),
        "orderPx": "-1", "triggerPxType": "last", "algoClOrdId": cl, "attachAlgoOrds": [DX._attach(inst, px * 0.45, px * 0.6)]}
if ps != "net": body["posSide"] = ps
aid = X.post("/api/v5/trade/order-algo", body)["data"][0]["algoId"]
t = next(x for x in DX.trigger_orders(inst) if x["algoId"] == aid); at = t["attachAlgoOrds"][0]
print(f"1) 测试条件单 {aid} live：触发 {t['triggerPx']} 止损 {at['slTriggerPx']} 止盈 {at['tpTriggerPx']}")
X.post("/api/v5/trade/cancel-algos", [{"instId": inst, "algoId": aid}]); time.sleep(1)
print("   已撤：", aid not in {x["algoId"] for x in DX.trigger_orders(inst)})
X.log_event("test_trigger", inst_id=inst, algoId=aid, result="placed_and_cancelled")
# 2) 市价最小张数开 + 附带止损止盈 → 平
body = {"instId": inst, "tdMode": "isolated", "side": "buy", "ordType": "market", "sz": mn, "attachAlgoOrds": [DX._attach(inst, px * 0.9, px * 1.1)]}
if ps != "net": body["posSide"] = ps
oid = X.post("/api/v5/trade/order", body)["data"][0]["ordId"]; time.sleep(2)
p = [x for x in DX.demo_positions() if x["instId"] == inst and x["posSide"] == ps]
al = [a for a in DX.close_algos(inst) if a.get("posSide") == ps]
print(f"2) 市价单 {oid}：持仓 {p[0]['pos'] if p else 0} 张 均价 {p[0]['avgPx'] if p else '-'}；附带止损 {[a.get('slTriggerPx') for a in al]} 止盈 {[a.get('tpTriggerPx') for a in al]}")
cb = {"instId": inst, "mgnMode": "isolated", "autoCxl": True}
if ps != "net": cb["posSide"] = ps
X.post("/api/v5/trade/close-position", cb); time.sleep(2)
X.log_event("test_market", inst_id=inst, ordId=oid, result="opened_and_closed")
# 3) 残留
pos = DX.demo_positions(); trig = DX.trigger_orders(); ca = DX.close_algos(); po = X.get("/api/v5/trade/orders-pending", instType="SWAP")["data"]
print(f"3) 残留：持仓 {len(pos)}，平仓算法单 {len(ca)}，普通挂单 {len(po)}；条件单 {[(x['algoId'], x['instId'], x['sz'], x['triggerPx']) for x in trig]}"
      f"（测试前已存在的保持：{before == {x['algoId'] for x in trig}}）")
print(f"   实时 USDT 权益 {DX.usdt_equity():.4f}")
