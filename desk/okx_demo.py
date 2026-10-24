"""OKX 官方模拟盘（Demo Trading）底层客户端（签名、请求、自检、合约换算）。执行逻辑见 desk/demo_exec.py。

!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
!! 禁止实盘：本模块只允许连接 OKX 模拟盘。                                                 !!
!!  - 只读取 OKX_DEMO_API_KEY / OKX_DEMO_SECRET_KEY / OKX_DEMO_PASSPHRASE，绝不读取实盘   !!
!!    OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSPHRASE，也不得回退到它们。                !!
!!  - 每个请求都强制带 x-simulated-trading: 1（在 _request 中硬编码，不可配置）。          !!
!!  - 启动时先用 demo key 调 /account/config 确认是模拟环境；失败即拒绝下单。              !!
!!  - 不得打印/记录任何密钥。                                                              !!
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!

"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import hmac
import json
import math
import os
import sys
import time
import uuid

import requests

from desk import settings

BASE_URL = "https://www.okx.com"   # 模拟盘与实盘同域名，靠 x-simulated-trading: 1 区分；该头在 _request 中强制
KEY_ENVS = ("OKX_DEMO_API_KEY", "OKX_DEMO_SECRET_KEY", "OKX_DEMO_PASSPHRASE")   # 禁止实盘：只读这三个
EVENTS = settings.STATE / "ledger" / "demo_events.jsonl"

_verified: bool | None = None   # 本进程内的模拟环境确认结果
_inst_cache: dict = {}


class DemoError(RuntimeError):
    pass


EVENT_SOURCES = ("strategy", "rediscuss", "ops", "test")   # O3：事件来源；每日下单数护栏只统计 strategy 与 rediscuss


def event_source() -> str:
    """当前进程的事件来源：环境变量 TA_EVENT_SOURCE（rediscuss/scripts/测试在入口处设置），默认 strategy。"""
    s = os.environ.get("TA_EVENT_SOURCE") or "strategy"
    return s if s in EVENT_SOURCES else "ops"


def set_event_source(src: str):
    assert src in EVENT_SOURCES, src
    os.environ["TA_EVENT_SOURCE"] = src


def log_event(kind: str, **kw):
    rec = {"time": settings.now_str(), "event": kind, **kw}
    rec.setdefault("source", event_source())
    try:
        settings.append_jsonl(EVENTS, rec)
    except Exception:
        pass
    print(f"[demo] {kind} {json.dumps(kw, ensure_ascii=False, default=str)[:400]}", file=sys.stderr)


def _keys():
    vals = [os.environ.get(k, "") for k in KEY_ENVS]
    if not all(vals):
        raise DemoError("OKX_DEMO_* 环境变量未全部设置（禁止回退实盘 key）")
    return vals


def _request(method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
    key, secret, pp = _keys()
    if params:
        path = path + "?" + "&".join(f"{k}={v}" for k, v in params.items())
    data = json.dumps(body) if body is not None else ""
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(time.time()*1000)%1000:03d}Z"
    sign = base64.b64encode(hmac.new(secret.encode(), (ts + method + path + data).encode(), hashlib.sha256).digest()).decode()
    headers = {"OK-ACCESS-KEY": key, "OK-ACCESS-SIGN": sign, "OK-ACCESS-TIMESTAMP": ts, "OK-ACCESS-PASSPHRASE": pp,
               "Content-Type": "application/json",
               "x-simulated-trading": "1"}   # 禁止实盘：强制模拟盘，任何调用路径都不能去掉
    r = requests.request(method, BASE_URL + path, headers=headers, data=data or None, timeout=15)
    try:
        j = r.json()
    except Exception:
        raise DemoError(f"HTTP {r.status_code} 非 JSON 响应")
    if str(j.get("code")) != "0":
        details = [{k: d.get(k) for k in ("sCode", "sMsg") if k in d} for d in (j.get("data") or []) if isinstance(d, dict)]
        raise DemoError(f"{method} {path.split('?')[0]} code={j.get('code')} msg={j.get('msg')} {details}")
    return j


def get(path, **params):
    return _request("GET", path, params=params or None)


class HaltError(DemoError):
    """HALT（state/HALT）生效时拒绝自动写操作。"""


def post(path, body, allow_halt: bool = False):
    """所有写操作的唯一出口。R7：HALT 生效时除 allow_halt=True 的撤开仓条件单（R10 停机撤单）外一律拒绝。"""
    if not allow_halt and settings.halt_active():
        log_event("halt_blocked_write", path=path)
        raise HaltError(f"HALT 全冻结生效（{settings.HALT_PATH}），拒绝写操作 {path}")
    if path == "/api/v5/trade/order" and isinstance(body, dict):   # D1.4：guardrails.allowed_ord_types 对普通单强制执行
        allowed = (settings.load_config().get("guardrails") or {}).get("allowed_ord_types") or []
        if body.get("ordType") not in allowed:
            log_event("ord_type_blocked", ordType=body.get("ordType"), allowed=allowed)
            raise DemoError(f"ordType={body.get('ordType')} 不在 guardrails.allowed_ord_types {allowed}")
    return _with_intent(path, body, lambda: _request("POST", path, body=body))


INTENTS = settings.STATE / "intents.jsonl"
INTENT_PATHS = ("/api/v5/trade/order", "/api/v5/trade/order-algo")


def _with_intent(path, body, send):
    """R11.2 意图记录：带 clOrdId/algoClOrdId 的下单先写 intent，再发送，成功写 ack（含交易所单号），异常写 error。
    请求超时等结果不明的情况由 desk/reconcile.py 按 clOrdId 向交易所对账。"""
    cl = body.get("clOrdId") or body.get("algoClOrdId") if isinstance(body, dict) else None
    if path not in INTENT_PATHS or not cl:
        return send()
    base = {"cl": cl, "path": path, "inst_id": body.get("instId"), "ordType": body.get("ordType"), "sz": body.get("sz"),
            "side": body.get("side"), "reduceOnly": bool(body.get("reduceOnly")), "source": event_source()}
    settings.append_jsonl(INTENTS, {"time": settings.now_str(), "state": "intent", **base})
    try:
        j = send()
    except Exception as e:
        settings.append_jsonl(INTENTS, {"time": settings.now_str(), "state": "error", "error": str(e)[:200], **base})
        raise
    d = (j.get("data") or [{}])[0]
    settings.append_jsonl(INTENTS, {"time": settings.now_str(), "state": "ack", "ordId": d.get("ordId"), "algoId": d.get("algoId"), **base})
    return j


def verify() -> bool:
    """启动自检：用 demo key 调一次 /account/config（带模拟盘头）。失败则本进程拒绝下单。"""
    global _verified
    if _verified is not None:
        return _verified
    try:
        c = get("/api/v5/account/config")["data"][0]
        _verified = True
        log_event("verify_ok", posMode=c.get("posMode"), acctLv=c.get("acctLv"))
    except Exception as e:
        _verified = False
        log_event("verify_failed", error=str(e))
    return _verified


def pos_mode() -> str:
    return get("/api/v5/account/config")["data"][0].get("posMode", "net_mode")


def instrument(inst: str) -> dict:
    if inst not in _inst_cache:
        _inst_cache[inst] = get("/api/v5/public/instruments", instType="SWAP", instId=inst)["data"][0]
    return _inst_cache[inst]


def _dec(x: float) -> str:
    return f"{x:.10f}".rstrip("0").rstrip(".")


def contracts(inst: str, qty_coin: float) -> str | None:
    """币数量 → 张数：qty/ctVal，按 lotSz 向下取整；低于 minSz 返回 None。"""
    i = instrument(inst)
    ct, lot, mn = float(i["ctVal"]), float(i["lotSz"]), float(i["minSz"])
    n = math.floor(qty_coin / ct / lot + 1e-9) * lot
    return _dec(n) if n + 1e-12 >= mn else None


def _px(inst: str, p) -> str:
    tick = float(instrument(inst)["tickSz"])
    return _dec(round(float(p) / tick) * tick)


def startup(cfg):
    """日报/再讨论/轮询启动时调用：模拟环境自检（失败则本进程拒绝下单）。"""
    verify()


# ------------------------------------------------------------------ 命令行
def _status():
    bal = get("/api/v5/account/balance", ccy="USDT")["data"][0]
    usdt = next((d for d in bal.get("details", []) if d.get("ccy") == "USDT"), {})
    pos = [{k: p.get(k) for k in ("instId", "posSide", "pos", "mgnMode", "lever", "avgPx")}
           for p in get("/api/v5/account/positions", instType="SWAP")["data"] if float(p.get("pos") or 0)]
    ords = get("/api/v5/trade/orders-pending", instType="SWAP")["data"]
    algos = sum(len(get("/api/v5/trade/orders-algo-pending", ordType=t, instType="SWAP")["data"]) for t in ("conditional", "oco", "trigger"))
    return {"posMode": pos_mode(), "usdt_eq": usdt.get("eq"), "usdt_avail": usdt.get("availBal"),
            "positions": pos, "pending_orders": len(ords), "pending_algos": algos}


if __name__ == "__main__":
    a = sys.argv[1:] or ["status"]
    if a[0] == "status":
        print(json.dumps({"verified": verify(), **_status()}, ensure_ascii=False, indent=1))
    else:
        print("用法: python -m desk.okx_demo status")
