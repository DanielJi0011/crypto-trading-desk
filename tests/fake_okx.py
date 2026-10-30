"""测试用内存版 OKX 模拟盘（只在测试里替换 okx_demo.get/post，不发任何网络请求）。"""
import itertools


class FakeOKX:
    def __init__(self):
        self.algos = []          # 挂着的 oco/conditional/trigger
        self.orders = []         # 普通挂单
        self.calls = []
        self.fail = {}           # path -> 异常（一次性或持续）
        self.amend_code = None   # 例如 "51506"
        self._id = itertools.count(9000)

    def install(self, monkeypatch):
        from desk import okx_demo as X, demo_exec as DX
        monkeypatch.setattr(X, "get", self.get)
        monkeypatch.setattr(X, "post", self.post)
        monkeypatch.setattr(X, "_px", lambda inst, v: f"{float(v):.2f}")
        monkeypatch.setattr(X, "_dec", lambda v: f"{float(v):g}")
        monkeypatch.setattr(DX, "_require", lambda: None)
        import time
        monkeypatch.setattr(time, "sleep", lambda s: None)
        return self

    def get(self, path, **kw):
        self.calls.append(("GET", path, kw))
        if path.endswith("orders-algo-pending"):
            t = kw.get("ordType")
            return {"data": [dict(a) for a in self.algos if a["ordType"] == t and a["instId"] == kw.get("instId", a["instId"])]}
        if path.endswith("orders-pending"):
            return {"data": [dict(o) for o in self.orders]}
        return {"data": []}

    def post(self, path, body):
        from desk import okx_demo as X
        self.calls.append(("POST", path, body))
        err = self.fail.get(path)
        if err:
            raise err
        if path.endswith("amend-algos"):
            if self.amend_code:
                raise X.DemoError(f"POST {path} code=1 msg= [{{'sCode': '{self.amend_code}'}}]")
            for a in self.algos:
                if a["algoId"] == body["algoId"]:
                    if "newSlTriggerPx" in body:
                        a["slTriggerPx"] = body["newSlTriggerPx"]
                    for at in body.get("attachAlgoOrds") or []:   # 条件入场单的附带止盈止损改价
                        a.setdefault("attachAlgoOrds", [{}])
                        if "newSlTriggerPx" in at: a["attachAlgoOrds"][0]["slTriggerPx"] = at["newSlTriggerPx"]
                        if "newTpTriggerPx" in at: a["attachAlgoOrds"][0]["tpTriggerPx"] = at["newTpTriggerPx"]
            return {"data": [{}]}
        if path.endswith("order-algo"):
            aid = str(next(self._id))
            self.algos.append({**body, "algoId": aid})
            return {"data": [{"algoId": aid, "sCode": "0"}]}
        if path.endswith("cancel-algos"):
            ids = {x["algoId"] for x in body}
            self.algos = [a for a in self.algos if a["algoId"] not in ids]
            return {"data": [{}]}
        return {"data": [{}]}
