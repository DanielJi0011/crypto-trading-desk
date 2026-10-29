"""OKX 模拟盘 WebSocket 常驻监听（只监听，不下单）。

!! 禁止实盘：只用 OKX_DEMO_* 三个 key，只连模拟盘地址 wspap.okx.com（见 URLS，硬编码不可配置）。
!! 不打印、不记录任何密钥（登录消息不入日志）。

依据 OKX 官方文档（docs-v5，2026-10 查证）：
  - 模拟盘 Private WS: wss://wspap.okx.com/ws/v5/private（文档现行地址；:8443 在本机握手超时）  → orders / positions / balance_and_position
  - 模拟盘 Business WS: wss://wspap.okx.com/ws/v5/business → orders-algo / algo-advance（算法单频道在 business）
  - 登录：sign = Base64(HmacSHA256(timestamp + 'GET' + '/users/self/verify', secret))，timestamp 为 Unix 秒
  - 心跳：30 秒内无数据会断开，发送字符串 'ping'，收到 'pong'
用法：python -m desk.demo_ws          （由 run_demo_ws.sh 常驻）
"""
from __future__ import annotations

import asyncio
import base64
import fcntl
import hashlib
import hmac
import json
import os
import random
import threading
import time
import traceback

from desk import settings

URLS = {   # 禁止实盘：只允许模拟盘域名
    "private": "wss://wspap.okx.com/ws/v5/private",
    "business": "wss://wspap.okx.com/ws/v5/business",
}
for _u in URLS.values():
    assert "wspap.okx.com" in _u, "禁止实盘"
KEY_ENVS = ("OKX_DEMO_API_KEY", "OKX_DEMO_SECRET_KEY", "OKX_DEMO_PASSPHRASE")
SUBS = {
    "private": [{"channel": "orders", "instType": "SWAP"},
                {"channel": "positions", "instType": "SWAP"},
                {"channel": "balance_and_position"}],          # 文档：该频道无 instType 参数
    "business": [{"channel": "orders-algo", "instType": "SWAP"},
                 {"channel": "algo-advance", "instType": "SWAP"}],
}
PING_EVERY = 25
BACKOFF_MAX = 60
ALERT_AFTER = 1800   # 连续断线超过 30 分钟仍未恢复才 POST 一次；30 分钟内自愈的只写本地日志

EVENTS = settings.STATE / "ledger" / "demo_ws_events.jsonl"
SEEN = settings.STATE / "ledger" / "demo_ws_seen.json"
HEARTBEAT = settings.STATE / "demo_ws_heartbeat"
SECRETS = settings.STATE / "secrets.env"
NOTIFY_KINDS = {"trigger_placed",   # 挂出入场条件单（早场/晚场/止损后/止盈后再讨论），按 algoId 只提醒一次
                "fill", "stop_loss", "take_profit", "liquidation", "trigger_fired", "disconnect_alert",
                "position_open", "position_closed",
                # 整改第一批新增：R5 风控、R10 停机撤单、Q5 过期只通知、O1/O2 日流程失败
                "daily_loss_halt", "drawdown_kill", "trigger_revoked", "trigger_expired", "position_expired", "position_extended", "expiry_discussion_started", "daily_failed", "daily_skipped",
                # 第二批：止损先挂新再撤旧失败、旧单残留、止损覆盖缺失（R11.4）
                "stop_replace_failed", "stop_replace_old_left", "stop_coverage_missing",
                # 第三批 L4：X 讨论摘录缺失（每天一次）
                "x_feed_missing",
                # 第四批 O1/O2：日流程缺失/完成、健康检查过期、KILL/HALT 开关变化、链路异常、可选存活心跳（默认关）
                "daily_missing", "daily_done", "healthcheck_stale", "kill_on", "kill_off", "halt_on", "halt_off",
                "chain_unhealthy", "heartbeat",
                # 离机备份与心跳失败
                "offsite_backup_failed", "offsite_heartbeat_failed",
                "evening_missing",
                # demo_ws 进程运行的代码落后于 HEAD（每小时至多一次）
                "stale_code"}
_lock = threading.Lock()
NOTIFY_TIMEOUT = 30
NOTIFY_RETRIES = 2
REVIEW_EVERY = 30   # R10：KILL/HALT 等风险状态检查周期（秒）
PROC_START_MS = int(time.time() * 1000)   # 本进程启动时间：早于它的事件视为补推/补查
REPLAY_SLACK_MS = 60_000
NOTIFIED_FILLS = settings.STATE / "ledger" / "notified_fills.json"   # 已发过成交类通知的 ordId（防重连补推重发）
COOLDOWN_PATH = settings.STATE / "notify_cooldown.json"   # 非交易故障告警按 event_type 冷却
# 非交易故障/链路类告警：同一 event_type 冷却窗口（notify.alert_cooldown_hours，默认 6 小时）内最多发一次。
# 成交/止损/止盈/条件单/开平仓/风控开关/止损覆盖类不在此列，永不冷却。
ALERT_COOLDOWN_KINDS = {"stale_code", "healthcheck_stale", "evening_missing", "daily_missing", "heartbeat",
                        "chain_unhealthy", "offsite_backup_failed", "offsite_heartbeat_failed", "disconnect_alert",
                        "x_feed_missing", "daily_failed", "daily_skipped"}
DEFAULT_COOLDOWN_H = 6.0
RUN_BRANCH_DEFAULT = "chore/publish-prep"
MERGE_KINDS = ("stop_loss", "take_profit", "trigger_fired")   # 触发后不立即通知，等整单成交合并成一条


def log(msg: str):
    settings.LOGS.mkdir(parents=True, exist_ok=True)
    line = f"[demo_ws {settings.now_str()}] {msg}"
    with open(settings.LOGS / f"demo-ws-{settings.today()}.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ------------------------------------------------------------------ 解析
def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def parse(msg: dict) -> list[dict]:
    """把一条推送解析成 0..n 个标准事件 {kind, key, inst_id, side, sz, px, pnl, ordId, algoId, state, raw_channel}。"""
    arg = msg.get("arg") or {}
    ch = arg.get("channel")
    out = []
    data = msg.get("data") or []
    if isinstance(data, dict):
        data = [data]
    for d in data:
        if not isinstance(d, dict):   # 非对象元素跳过（防御 'str' object has no attribute 'get'）
            continue
        if ch == "orders":
            st = d.get("state")
            cat = d.get("category") or "normal"
            base = {"inst_id": d.get("instId"), "side": d.get("side"), "posSide": d.get("posSide"),
                    "sz": d.get("fillSz") or d.get("accFillSz"), "px": d.get("fillPx") or d.get("avgPx"),
                    "pnl": d.get("fillPnl") or d.get("pnl"), "ordId": d.get("ordId"), "algoId": d.get("algoId") or None,
                    "state": st, "category": cat, "channel": ch,
                    "accFillSz": d.get("accFillSz"), "avgPx": d.get("avgPx"), "reduceOnly": d.get("reduceOnly"),
                    "uTime": d.get("uTime") or d.get("fillTime")}
            if cat in ("full_liquidation", "partial_liquidation", "adl"):
                if st in ("filled", "partially_filled"):
                    out.append({**base, "kind": "liquidation", "key": f"liq:{d.get('ordId')}:{st}:{d.get('tradeId') or ''}"})
            elif st == "canceled" and (_f(d.get("accFillSz")) or 0) > 0:   # 部分成交后撤单 → 视为终态，合并通知
                out.append({**base, "sz": None, "px": None, "pnl": None,
                            "kind": "fill", "key": f"ord:{d.get('ordId')}:canceled"})
            elif st in ("filled", "partially_filled"):
                kind = "fill" if st == "filled" else "partial_fill"
                out.append({**base, "kind": kind, "key": f"ord:{d.get('ordId')}:{st}:{d.get('tradeId') or d.get('accFillSz') or ''}"})
        elif ch in ("orders-algo", "algo-advance"):
            st = d.get("state")
            if st != "effective":      # live/canceled/order_failed/partially_effective 不算触发
                continue
            act = d.get("actualSide") or ""
            ot = d.get("ordType")
            kind = "stop_loss" if act == "sl" else "take_profit" if act == "tp" else "trigger_fired"
            out.append({"kind": kind, "key": f"algo:{d.get('algoId')}:{st}", "inst_id": d.get("instId"),
                        "side": d.get("side"), "posSide": d.get("posSide"), "sz": d.get("actualSz") or d.get("sz"),
                        "px": d.get("actualPx") or d.get("triggerPx"), "pnl": None, "ordId": d.get("ordId") or None,
                        "algoId": d.get("algoId"), "state": st, "ordType": ot, "channel": ch})
        elif ch == "positions":
            pos = _f(d.get("pos"))
            pid = d.get("posId") or f"{d.get('instId')}:{d.get('posSide')}"
            out.append({"kind": "position_update", "key": None, "pos_id": pid, "pos": pos,
                        "inst_id": d.get("instId"), "side": d.get("posSide"), "px": d.get("avgPx"),
                        "pnl": d.get("realizedPnl") or d.get("upl"), "channel": ch,
                        "cTime": d.get("cTime"), "uTime": d.get("uTime"), "posSide": d.get("posSide")})
        elif ch == "balance_and_position":
            if d.get("eventType") == "liquidation":
                for p in [x for x in (d.get("posData") or []) if isinstance(x, dict)]:
                    out.append({"kind": "liquidation", "key": f"liq:bp:{p.get('posId')}:{d.get('pTime')}",
                                "inst_id": p.get("instId"), "side": p.get("posSide"), "sz": p.get("pos"),
                                "px": p.get("avgPx"), "pnl": None, "ordId": p.get("tradeId"), "algoId": None,
                                "state": "liquidation", "channel": ch})
    return out


POS_FIELDS = ("pos_snap", "pos_snap_ms", "pos_notified", "fill_marks", "ws_down")
POS_GRACE = 15          # ws 推送的开/平仓延迟这么多秒再判定，等同一笔的成交/触发通知先到，避免重复
FILL_COVER_MS = 600_000  # 同品种同方向同动作的成交类通知在 10 分钟内已发 → 视为已覆盖


@__import__("contextlib").contextmanager
def _file_lock(path):
    lp = path.with_suffix(path.suffix + ".lock")
    lp.parent.mkdir(parents=True, exist_ok=True)
    with open(lp, "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _rmw(path, fn):
    """跨进程加锁读改写 seen 文件，只动 fn 修改的字段。"""
    with _file_lock(path):
        j = settings.read_json(path, {})
        r = fn(j)
        settings.write_json(path, j)
        return r


class State:
    """去重：事件 key（ordId/algoId+状态）持久化；持仓按 posId 记上次是否非零，用于识别新增/归零。"""

    def __init__(self, path=None):
        self.path = path or SEEN
        j = settings.read_json(self.path, {"keys": [], "pos": {}})
        self.keys = list(j.get("keys", []))
        self._set = set(self.keys)
        self.pos = dict(j.get("pos", {}))
        self.orders = dict(j.get("orders", {}))   # ordId -> {pnl, n}：各笔成交已实现盈亏累计
        self.algos = dict(j.get("algos", {}))     # algoId -> 已触发但等待成交合并通知的止损/止盈/触发事件
        self.done_algos = list(j.get("done_algos", []))   # 已发过合并通知的 algoId

    def save(self):
        self.keys = self.keys[-5000:]
        self._set = set(self.keys)
        self.done_algos = self.done_algos[-2000:]
        if len(self.orders) > 2000:
            self.orders = dict(list(self.orders.items())[-2000:])
        with _file_lock(self.path):   # 持仓通知字段（POS_FIELDS）由 demo_watch 等其他进程共写，保留文件中的最新值
            cur = settings.read_json(self.path, {})
            out = {"keys": self.keys, "pos": self.pos, "orders": self.orders,
                   "algos": self.algos, "done_algos": self.done_algos}
            out.update({k: cur[k] for k in POS_FIELDS if k in cur})
            settings.write_json(self.path, out)

    def accept(self, ev: dict) -> dict | None:
        """返回需处理的事件（去重后）；持仓更新转换为 position_open / position_closed，否则丢弃。"""
        if ev["kind"] == "position_update":
            pid, nz = ev["pos_id"], bool(ev["pos"])
            prev = self.pos.get(pid)
            self.pos[pid] = nz
            if prev is None and not nz:
                return None
            if nz and not prev:
                return {**ev, "kind": "position_open", "key": f"pos:{pid}:open:{int(time.time())}"}
            if prev and not nz:
                return {**ev, "kind": "position_closed", "key": f"pos:{pid}:closed:{int(time.time())}"}
            return None
        if ev["key"] in self._set:
            return None
        self.keys.append(ev["key"]); self._set.add(ev["key"])
        return ev


# ------------------------------------------------------------------ 通知
def _notify_cfg():
    url, key = os.environ.get("TA_NOTIFY_WEBHOOK_URL", ""), os.environ.get("TA_NOTIFY_WEBHOOK_KEY", "")
    if not url and SECRETS.exists():   # 允许之后再写入 secrets.env，无需重启；只取这两个变量
        for ln in SECRETS.read_text().splitlines():
            k, _, v = ln.strip().removeprefix("export ").partition("=")
            v = v.strip().strip('"').strip("'")
            if k == "TA_NOTIFY_WEBHOOK_URL":
                url = v
            elif k == "TA_NOTIFY_WEBHOOK_KEY":
                key = key or v
    return url, key


def _drill(inst) -> bool:
    try:
        from desk import drill
        return drill.active(inst)
    except Exception:
        return False


def payload(ev: dict) -> dict:
    return {"event_type": ev.get("kind"), "coin": (ev.get("inst_id") or "").split("-")[0] or None,
            "inst_id": ev.get("inst_id"), "side": ev.get("posSide") if ev.get("posSide") not in (None, "", "net") else ev.get("side"),
            "size": ev.get("sz"), "fill_price": ev.get("px"), "pnl": ev.get("pnl"),
            "order_id": ev.get("ordId"), "algo_id": ev.get("algoId"),
            "rediscuss_started": ev.get("rediscuss") == "已启动", "rediscuss_result": ev.get("rediscuss"),
            "time": settings.now_str(), "timezone": "Asia/Shanghai (北京时间)", "source": "okx-demo-ws",
            "message": (("【演练】" if _drill(ev.get("inst_id")) else "") + (ev.get("message") or "")) or None,
            "acc_fill_size": ev.get("accFillSz"), "avg_price": ev.get("avgPx"), "realized_pnl_total": ev.get("pnl_total"),
            "fill_count": ev.get("fill_count"), "action": ev.get("action"), "order_state": ev.get("order_state"),
            "trigger_type": ev.get("trigger_kind"),
            "event_time": ev.get("event_time"), "offline": ev.get("offline"), "detected_by": ev.get("detected_by"),
            "pos_id": ev.get("pos_id"),
            "trigger_price": ev.get("trigger_px"), "stop_loss": ev.get("stop_loss"), "take_profit": ev.get("take_profit"),
            "decision_id": ev.get("decision_id"), "session": ev.get("session")}


def _action(ev: dict) -> str:
    """开仓/平仓：双向持仓看 side+posSide；单向(net)看 reduceOnly 或是否有已实现盈亏。"""
    ps, sd = ev.get("posSide"), ev.get("side")
    if ps == "long":
        return "开仓" if sd == "buy" else "平仓"
    if ps == "short":
        return "开仓" if sd == "sell" else "平仓"
    if str(ev.get("reduceOnly")).lower() == "true" or (_f(ev.get("pnl_total")) or 0) != 0:
        return "平仓"
    return "开仓"


def _merge(ev: dict, st: "State") -> dict | None:
    """在锁内调用。partial_fill 累计盈亏并返回 None（不通知）；终态 fill 返回合并后的通知事件。"""
    k = ev["kind"]
    if k in ("partial_fill", "fill"):
        o = st.orders.setdefault(str(ev.get("ordId")), {"pnl": 0.0, "n": 0})
        if ev.get("sz") or ev.get("pnl") not in (None, ""):
            o["pnl"] += _f(ev.get("pnl")) or 0.0
            o["n"] += 1
        if k == "partial_fill":
            return None
        out = {**ev, "pnl_total": round(o["pnl"], 10), "fill_count": o["n"], "order_state": ev.get("state"),
               "sz": ev.get("accFillSz") or ev.get("sz"), "px": ev.get("avgPx") or ev.get("px")}
        out["pnl"] = out["pnl_total"]
        out["action"] = _action(out)
        aid = ev.get("algoId")
        if aid and aid in st.algos:   # 止损/止盈已触发 → 合并成一条「已完成」通知
            trig = st.algos.pop(aid)
            out.update({"kind": trig["kind"], "trigger_kind": trig["kind"], "rediscuss": trig.get("rediscuss"),
                        "ordType": trig.get("ordType")})
            name = {"stop_loss": "止损", "take_profit": "止盈", "trigger_fired": "条件单"}[trig["kind"]]
            out["message"] = f"{name}已完成：{out['action']} {out['sz']} @ {out['px']}，已实现盈亏 {out['pnl_total']}"
        else:
            out["message"] = f"订单{'已成交' if ev.get('state') == 'filled' else '部分成交后撤单'}：{out['action']} {out['sz']} @ {out['px']}，已实现盈亏 {out['pnl_total']}"
        if aid:
            st.done_algos.append(aid)
        st.orders.pop(str(ev.get("ordId")), None)
        return out
    if k in MERGE_KINDS:
        if ev.get("algoId") in st.done_algos:   # 成交先于触发推送到达：成交通知已发，不再重复
            return None
        st.algos[ev["algoId"]] = {kk: ev.get(kk) for kk in ("kind", "rediscuss", "ordType", "inst_id", "px", "sz")}
        return None
    return ev


def _notify_section() -> dict:
    try:
        import yaml
        with open(settings.CONFIG, encoding="utf-8") as f:
            return (yaml.safe_load(f) or {}).get("notify") or {}
    except Exception:
        return {}


def cooldown_hours() -> float:
    try:
        return float(_notify_section().get("alert_cooldown_hours", DEFAULT_COOLDOWN_H))
    except (TypeError, ValueError):
        return DEFAULT_COOLDOWN_H


def cooldown_claim(kind: str, now: float | None = None, hours: float | None = None, path=None) -> bool:
    """非交易告警冷却：返回 True 表示可以发送（并登记时间）；冷却窗口内返回 False。交易类事件恒为 True。"""
    if kind not in ALERT_COOLDOWN_KINDS:
        return True
    now = time.time() if now is None else now
    win = (cooldown_hours() if hours is None else hours) * 3600

    def fn(j):
        last = float((j.get("last_sent") or {}).get(kind, 0) or 0)
        if win > 0 and now - last < win:
            j.setdefault("suppressed", {})[kind] = int((j.get("suppressed") or {}).get(kind, 0)) + 1
            return False
        j.setdefault("last_sent", {})[kind] = now
        return True
    return _rmw(path or COOLDOWN_PATH, fn)


def fill_claim(ev: dict, path=None) -> bool:
    """成交类通知按 ordId(+kind) 跨进程只发一次；重连补推的同一笔返回 False。无 ordId 时放行。"""
    oid = ev.get("ordId") or ev.get("algoId")
    if not oid:
        return True
    k = f"{ev.get('kind')}:{oid}"

    def fn(j):
        d = j.setdefault("ords", {})
        if k in d:
            return False
        d[k] = _now_ms()
        if len(d) > 5000:
            j["ords"] = dict(list(d.items())[-5000:])
        return True
    return _rmw(path or NOTIFIED_FILLS, fn)


def notify(ev: dict) -> bool:
    if ev.get("kind") not in NOTIFY_KINDS:
        return False
    if os.environ.get("TA_NOTIFY_DISABLED") == "1":   # 测试与演练用总开关：不发任何 webhook
        log(f"通知已禁用（TA_NOTIFY_DISABLED=1）：{ev.get('kind')}")
        return False
    url, key = _notify_cfg()
    if not url:
        log(f"通知未发送（TA_NOTIFY_WEBHOOK_URL 未设置）：{ev.get('kind')} {ev.get('inst_id')}")
        return False
    import requests
    h = {"Content-Type": "application/json"}
    if key:
        h["Authorization"] = f"Bearer {key}"; h["X-Webhook-Key"] = key
    try:
        if not cooldown_claim(ev.get("kind")):
            log(f"告警冷却中，跳过 {ev.get('kind')}（notify.alert_cooldown_hours）")
            return False
    except Exception as e:
        log(f"冷却检查失败 {type(e).__name__}，照常发送")
    body = json.dumps(payload(ev), ensure_ascii=False).encode()
    for i in range(1 + NOTIFY_RETRIES):
        try:
            r = requests.post(url, data=body, headers=h, timeout=NOTIFY_TIMEOUT)
            log(f"通知已发送 {ev.get('kind')} {ev.get('inst_id')} HTTP {r.status_code}（第{i + 1}次）")
            if r.ok or r.status_code < 500:
                return r.ok
        except Exception as e:
            log(f"通知失败 {ev.get('kind')}（第{i + 1}次）: {type(e).__name__}")
        if i < NOTIFY_RETRIES:
            time.sleep(2 * (i + 1))
    return False


# ------------------------------------------------------------------ 挂单提醒（trigger_placed / entry_limit_registered 同一 algoId 只提醒一次）
PLACED_SEEN = settings.STATE / "ledger" / "trigger_placed_notified.json"   # 独立文件：不受常驻 demo_ws 的 State.save 覆盖


def session_label(source=None, session=None) -> str:
    """来源场次中文：post_stop:* 止损后再讨论；post_tp:* 止盈后再讨论；否则按 settings.SESSION 早场/晚场。"""
    src = str(source or "")
    if src.startswith("post_stop"):
        return "止损后再讨论"
    if src.startswith("post_tp"):
        return "止盈后再讨论"
    s = session or getattr(settings, "SESSION", None)
    return {"morning": "8点早场", "evening": "20:30晚场"}.get(s, src or str(s or "未知"))


def placed_event(meta: dict, session=None) -> dict:
    inst = meta.get("inst_id") or meta.get("inst") or ""
    side = meta.get("side")
    cn = {"long": "做多", "short": "做空"}.get(side, str(side))
    label = session_label(meta.get("source"), session)
    sz = meta.get("sz")
    tp = meta.get("take_profit")
    msg = (f"【挂单提醒】{inst.split('-')[0]} {cn} 入场条件单已挂出：触发价 {meta.get('trigger_px')}，{sz} 张，"
           f"止损 {meta.get('stop_loss')}，止盈 {tp if tp not in (None, '') else '无'}；"
           f"决策 {meta.get('decision_id')}；来源 {label}；algoId {meta.get('algoId')}")
    return {"kind": "trigger_placed", "inst_id": inst, "side": side, "posSide": side, "sz": sz,
            "trigger_px": meta.get("trigger_px"), "stop_loss": meta.get("stop_loss"), "take_profit": tp,
            "decision_id": meta.get("decision_id"), "algoId": str(meta.get("algoId")), "session": label,
            "px": meta.get("order_px") if meta.get("order_px") not in (None, "", "-1") else None, "message": msg}


def notify_placed(meta: dict, session=None, notifier=None, path=None) -> bool:
    """挂出入场条件单后即时提醒；同一 algoId 跨进程加锁只提醒一次（发送失败则释放，允许之后重试）。不下单、不抛异常。"""
    aid = str(meta.get("algoId") or "")
    if not aid or aid == "None":
        return False
    path = path or PLACED_SEEN
    notifier = notifier or notify

    def claim(j):
        d = j.setdefault("algos", {})
        if aid in d:
            return False
        d[aid] = settings.now_str()
        if len(d) > 2000:
            j["algos"] = dict(list(d.items())[-2000:])
        return True
    try:
        if not _rmw(path, claim):
            return False
        ok = notifier(placed_event(meta, session))
        if ok is False:
            _rmw(path, lambda j: j.setdefault("algos", {}).pop(aid, None))
        return ok is not False
    except Exception as e:
        log(f"挂单提醒异常 {type(e).__name__}")
        return False


# ------------------------------------------------------------------ 处理
def handle(ev: dict, st: State, cfg=None, rediscuss=None, notifier=notify) -> dict | None:
    if ev["kind"] == "position_update":   # 开/平仓通知统一走 posId+cTime 去重（见 ws_position）
        try:
            ws_position(ev, path=st.path, notifier=notifier)
        except Exception as e:
            log(f"持仓变化处理失败 {type(e).__name__}: {e}")
    with _lock:
        ev = st.accept(ev)
        if not ev:
            return None
        if ev["kind"] in ("stop_loss", "take_profit"):
            if ev["kind"] == "stop_loss":   # 止盈不记止损冷却（不禁止同向）
                try:   # L6：记下被止损方向，冷却期内同向不再开仓
                    from desk import stop_cooldown
                    stop_cooldown.record(ev.get("inst_id"), ev.get("side"), ev.get("posSide"), ev.get("algoId"))
                except Exception as e:
                    log(f"止损冷却记录失败 {type(e).__name__}: {e}")
            if rediscuss is None:
                from desk.monitor import schedule_rediscuss as rediscuss
            if _drill(ev.get("inst_id")):   # 演练合约：不启动再讨论
                rediscuss = lambda _c, _t: "演练跳过"
            ev["rediscuss_reason"] = "tp" if ev["kind"] == "take_profit" else "sl"
            try:   # trade_id 与 demo_watch 一致：'DEMO-<algoId>'，共用 rediscuss.json 去重与冷却
                ev["rediscuss"] = rediscuss(cfg if cfg is not None else settings.load_config(),
                                            {"inst_id": ev["inst_id"], "trade_id": "DEMO-" + str(ev["algoId"]),
                                             "reason": ev["rediscuss_reason"]})
            except Exception as e:
                ev["rediscuss"] = f"错误 {type(e).__name__}: {e}"
        if ev["kind"] == "trigger_fired" and ev.get("algoId"):   # 限价入场：记下触发时间（只记录，不下单）
            try:
                from desk import entry_limit
                entry_limit.record_trigger(ev["algoId"], ord_id=ev.get("ordId"))
            except Exception as e:
                log(f"记录触发时间失败 {type(e).__name__}")
        rec = {"time": settings.now_str(), **ev}
        settings.append_jsonl(EVENTS, rec)
        nev = _merge(ev, st)
        st.save()
    log(f"事件 {ev['kind']} {ev.get('inst_id')} ord={ev.get('ordId')} algo={ev.get('algoId')} px={ev.get('px')} sz={ev.get('sz')}"
        + (f" 再讨论={ev['rediscuss']}" if "rediscuss" in ev else ""))
    if nev is None or nev["kind"] in ("position_open", "position_closed"):
        return ev
    try:
        fpath = st.path.parent / NOTIFIED_FILLS.name
        if not fill_claim(nev, path=fpath):
            log(f"重连补推的已通知成交 {nev['kind']} ord={nev.get('ordId')}，跳过 webhook")
            return ev
        ok = notifier(nev)
        if ok is False:
            oid = nev.get("ordId") or nev.get("algoId")
            _rmw(fpath, lambda j: j.setdefault("ords", {}).pop(f"{nev.get('kind')}:{oid}", None))
        if ok is not False:
            mark_fill(nev, path=st.path)
    except Exception as e:
        log(f"notify 异常 {type(e).__name__}")
    return ev


# ------------------------------------------------------------------ 开/平仓通知（ws 推送、重连补查、demo_watch 轮询共用）
def _now_ms():
    return int(time.time() * 1000)


def _fmt_ms(ms) -> str | None:
    try:
        import datetime as _dt
        return _dt.datetime.fromtimestamp(int(ms) / 1000, settings.TZ).strftime("%Y-%m-%d %H:%M:%S%z")
    except (TypeError, ValueError):
        return None


def _act_of(ev) -> str | None:
    if ev.get("kind") == "liquidation":
        return "close"
    a = ev.get("action")
    return {"开仓": "open", "平仓": "close"}.get(a)


def mark_fill(ev: dict, path=None):
    """成交/止损/止盈/条件单/强平通知发出后记下 品种:方向:动作，对应的开/平仓不再单独通知。"""
    act, inst = _act_of(ev), ev.get("inst_id")
    if not act or not inst:
        return
    k = f"{inst}:{ev.get('posSide') or ev.get('side') or 'net'}:{act}"
    now = _now_ms()

    def fn(j):
        fm = j.setdefault("fill_marks", {})
        fm[k] = now
        for kk in [kk for kk, t in fm.items() if now - t > 86_400_000]:
            fm.pop(kk)
    _rmw(path or SEEN, fn)


def pos_item(p: dict) -> dict:
    """OKX 持仓（REST /account/positions 或 positions 推送）→ 标准项。"""
    return {"pos_id": str(p.get("posId") or f"{p.get('instId')}:{p.get('posSide')}"), "c_time": str(p.get("cTime") or ""),
            "inst_id": p.get("instId"), "pos_side": p.get("posSide"), "sz": p.get("pos"), "avg_px": p.get("avgPx")}


def _pkey(it):
    return f"{it['pos_id']}:{it['c_time']}"


def ws_down_mark(name: str, start_ms: int | None = None, end_ms: int | None = None, path=None):
    """记录 ws 断线区间 [name, start, end]；end=None 表示仍断开。"""
    def fn(j):
        dl = j.setdefault("ws_down", [])
        if start_ms is not None and not any(d[0] == name and d[2] is None for d in dl):
            dl.append([name, int(start_ms), None])
        if end_ms is not None:
            for d in dl:
                if d[0] == name and d[2] is None:
                    d[2] = int(end_ms)
        j["ws_down"] = dl[-200:]
    _rmw(path or SEEN, fn)


def _offline(j, t0, t1=None) -> bool:
    """[t0, t1] 是否与任一 ws 断线区间重叠（t1 缺省即时间点 t0）。"""
    if t0 is None:
        return False
    t1 = t0 if t1 is None else t1
    now = _now_ms()
    return any(int(s) <= t1 and t0 <= (e if e is not None else now) for _, s, e in j.get("ws_down", []))


def _claim(j, ch: dict) -> dict | None:
    """在锁内：决定该开/平仓是否需要通知；需要则登记 pos_notified 并返回通知事件。"""
    nk = f"{ch['key']}:{ch['act']}"
    pn = j.setdefault("pos_notified", {})
    if nk in pn:
        return None
    ev_ms = ch.get("event_ms")
    if ev_ms and ev_ms < PROC_START_MS - REPLAY_SLACK_MS and any(
            k.startswith(f"{ch['pos_id']}:") and k.endswith(f":{ch['act']}") for k in pn):
        pn[nk] = {"at": ch["detected_ms"], "replay_skipped": True}   # 同一 pos_id 同一动作已提醒过的补推
        return None
    mark = (j.get("fill_marks") or {}).get(f"{ch['inst_id']}:{ch['pos_side'] or 'net'}:{ch['act']}")
    ref = ch.get("event_ms") or ch["detected_ms"]
    if mark and (abs(mark - ref) <= FILL_COVER_MS or 0 <= ch["detected_ms"] - mark <= FILL_COVER_MS):
        pn[nk] = {"at": ch["detected_ms"], "covered_by_fill": True}
        return None
    pn[nk] = {"at": ch["detected_ms"], "source": ch["source"]}
    if len(pn) > 2000:
        j["pos_notified"] = dict(list(pn.items())[-2000:])
    off = ch.get("offline")
    if off is None:
        off = _offline(j, ch.get("event_ms")) if ch.get("event_ms") else _offline(j, ch.get("since_ms") or ch["detected_ms"], ch["detected_ms"])
    name = "开仓" if ch["act"] == "open" else "平仓"
    side = {"long": "多", "short": "空"}.get(ch["pos_side"], ch["pos_side"] or "")
    et = _fmt_ms(ch.get("event_ms")) or _fmt_ms(ch["detected_ms"])
    msg = (f"{name}：{ch['inst_id']} {side} {ch['sz']}张 均价 {ch['avg_px']}，时间 {et}（北京时间）"
           + ("，发生在 WebSocket 断线期间" if off else "") + f"；发现途径：{ch['source']}")
    return {"kind": f"position_{'open' if ch['act'] == 'open' else 'closed'}", "inst_id": ch["inst_id"],
            "posSide": ch["pos_side"], "side": ch["pos_side"], "sz": ch["sz"], "px": ch["avg_px"],
            "pos_id": ch["pos_id"], "c_time": ch["c_time"], "event_time": et, "offline": bool(off),
            "detected_by": ch["source"], "message": msg}


def _send_changes(changes, path, notifier):
    sent = []
    for ch in changes:
        ev = _rmw(path, lambda j: _claim(j, ch))
        if not ev:
            log(f"持仓变化 {ch['act']} {ch['inst_id']} {ch['key']} 已通知或已由成交通知覆盖，跳过")
            continue
        log(f"持仓变化 {ev['kind']} {ev['inst_id']} {ev['posSide']} sz={ev['sz']} px={ev['px']} 断线期间={ev['offline']} 来源={ch['source']}")
        try:
            notifier(ev)
        except Exception as e:
            log(f"notify 异常 {type(e).__name__}")
        sent.append(ev)
    return sent


def _change(it, act, source, now, event_ms=None, since_ms=None, offline=None):
    return {**it, "key": _pkey(it), "act": act, "source": source, "detected_ms": now,
            "event_ms": event_ms, "since_ms": since_ms, "offline": offline}


def sync_snapshot(positions: list[dict], source: str, notifier=None, path=None, now_ms=None) -> list[dict]:
    """REST 全量持仓快照与上次快照对比：新增 → 开仓通知，消失 → 平仓通知。首次运行只建基线不通知（重启不会重报已有持仓）。"""
    path = path or SEEN
    notifier = notifier or notify
    now = now_ms or _now_ms()
    items = {_pkey(i): i for i in (pos_item(p) for p in positions) if (_f(i["sz"]) or 0) != 0}

    def fn(j):
        snap = j.get("pos_snap")
        prev_ms = j.get("pos_snap_ms")
        j["pos_snap"], j["pos_snap_ms"] = items, now
        if snap is None:
            return None
        ch = [_change(it, "open", source, now, event_ms=int(it["c_time"]) if it["c_time"].isdigit() else None)
              for k, it in items.items() if k not in snap]
        ch += [_change(it, "close", source, now, since_ms=prev_ms) for k, it in snap.items() if k not in items]
        return ch
    changes = _rmw(path, fn)
    if changes is None:
        log(f"持仓快照基线已建立（{len(items)} 个），不通知")
        return []
    return _send_changes(changes, path, notifier)


def ws_position(ev: dict, path=None, notifier=None, grace=None, now_ms=None):
    """positions 推送的单个持仓：未见过的 posId+cTime 且非零 → 开仓；快照中有、推送为零 → 平仓。
    通知延迟 grace 秒判定，让同一笔的成交/触发通知先登记（避免重复）。基线未建立时不处理（由 REST 快照建立）。"""
    path = path or SEEN
    notifier = notifier or notify
    now = now_ms or _now_ms()
    it = {"pos_id": str(ev.get("pos_id")), "c_time": str(ev.get("cTime") or ""), "inst_id": ev.get("inst_id"),
          "pos_side": ev.get("posSide") or ev.get("side"), "sz": ev.get("pos"), "avg_px": ev.get("px")}

    def fn(j):
        snap = j.get("pos_snap")
        if snap is None:
            return None
        if ev.get("pos"):
            it["sz"] = str(ev.get("pos")).removesuffix(".0") if isinstance(ev.get("pos"), float) else ev.get("pos")
            k = _pkey(it)
            new = k not in snap
            snap[k] = it
            if new:
                return _change(it, "open", "ws 推送", now, event_ms=int(it["c_time"]) if it["c_time"].isdigit() else None)
            return None
        for k, old in list(snap.items()):
            if old["pos_id"] == it["pos_id"]:
                snap.pop(k)
                u = str(ev.get("uTime") or "")
                return _change(old, "close", "ws 推送", now, event_ms=int(u) if u.isdigit() else None)
        return None
    ch = _rmw(path, fn)
    if not ch:
        return None
    g = POS_GRACE if grace is None else grace
    if g <= 0:
        return _send_changes([ch], path, notifier)
    threading.Timer(g, _send_changes, args=([ch], path, notifier)).start()
    return None


# ------------------------------------------------------------------ 连接
def login_msg() -> dict:
    vals = [os.environ.get(k, "") for k in KEY_ENVS]
    if not all(vals):
        raise RuntimeError("OKX_DEMO_* 未全部设置（禁止回退实盘 key）")
    key, secret, pp = vals
    ts = str(int(time.time()))
    sign = base64.b64encode(hmac.new(secret.encode(), (ts + "GET" + "/users/self/verify").encode(), hashlib.sha256).digest()).decode()
    return {"op": "login", "args": [{"apiKey": key, "passphrase": pp, "timestamp": ts, "sign": sign}]}


_hc_checked = 0.0
CATCHUP_TIMEOUT = 160
STALE_ALERT_EVERY = 3600   # 旧代码告警每小时至多一次
_code_rev = {"start": None, "alerted": 0.0}


def catchup_subprocess():
    import subprocess, sys
    return subprocess.run([sys.executable, "-m", "desk.demo_watch", "--once", "--source", "demo_ws 重连补查"],
                          cwd=str(settings.CODE), capture_output=True, text=True, timeout=CATCHUP_TIMEOUT)


def git_head() -> str | None:
    import subprocess
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(settings.CODE), capture_output=True,
                              text=True, timeout=10).stdout.strip() or None
    except Exception:
        return None


def git_branch() -> str | None:
    import subprocess
    try:
        b = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(settings.CODE), capture_output=True,
                           text=True, timeout=10).stdout.strip()
        return b or None   # detached 时为 "HEAD"
    except Exception:
        return None


def run_branch() -> str:
    return str(_notify_section().get("run_branch") or RUN_BRANCH_DEFAULT)


def check_code_version(now=None, head=None, notifier=None, branch=None) -> bool:
    """启动时记录代码版本；之后比对 HEAD，不同则说明本进程跑的是旧代码 → 写日志并通知（每小时至多一次）。返回是否旧代码。"""
    now = time.time() if now is None else now
    head = head or git_head()
    if _code_rev["start"] is None:
        _code_rev["start"] = head
        return False
    if not head or head == _code_rev["start"]:
        return False
    br = branch or git_branch()
    if br != run_branch():   # 未合并分支 / detached 上的 HEAD 变化不告警
        log(f"HEAD {head[:7]} 不在运行分支 {run_branch()}（当前 {br}），不发 stale_code")
        return False
    if now - _code_rev["alerted"] >= STALE_ALERT_EVERY:
        _code_rev["alerted"] = now
        msg = f"demo_ws 运行的是旧代码（启动版本 {str(_code_rev['start'])[:7]}，当前 HEAD {head[:7]}），需重启生效"
        log("告警：" + msg)
        try:
            (notifier or notify)({"kind": "stale_code", "message": msg})
        except Exception as e:
            log(f"stale_code 通知失败 {type(e).__name__}")
    return True


def beat():
    HEARTBEAT.parent.mkdir(parents=True, exist_ok=True)
    HEARTBEAT.write_text(settings.now_str() + "\n")
    global _hc_checked
    if time.time() - _hc_checked >= 300:              # O2：每 5 分钟看一次健康检查是否停摆
        _hc_checked = time.time()
        try:
            from desk import chain_health
            chain_health.healthcheck_stale()
        except Exception as e:
            log(f"healthcheck_stale 检查失败 {type(e).__name__}")
        try:
            check_code_version()
        except Exception as e:
            log(f"代码版本检查失败 {type(e).__name__}")


class Runner:
    def __init__(self):
        self.st = State()
        self.down_since: dict[str, float | None] = {k: None for k in URLS}
        self.alerted = False
        self.down_mono: dict[str, float | None] = {k: None for k in URLS}   # 断线起点（单调时钟，用于告警）
        self.ever_connected: set[str] = set()
        self.last_alive: dict[str, float | None] = {k: None for k in URLS}   # 最后一次收到数据/pong；挂起后据此算断线起点

    def _catchup(self):
        """重连补查：以子进程运行 python -m desk.demo_watch --once，保证用磁盘上的最新代码（不复用本进程内存里的旧模块）。"""
        def go():
            try:
                r = catchup_subprocess()
                log(f"重连补查（子进程）退出码 {r.returncode}；{((r.stdout or '') + (r.stderr or '')).strip()[-300:]}")
            except Exception as e:
                log(f"重连补查失败 {type(e).__name__}: {e}")
        threading.Thread(target=go, daemon=True).start()

    def alert_due(self, now_mono: float | None = None) -> int | None:
        """断线告警判定（纯逻辑，便于测试）：任一连接按单调时钟连续断线 > ALERT_AFTER 且本次断线未告警过 → 返回分钟数。
        用 monotonic 计时：整机暂停（virtual machine fork）期间不计入，避免恢复瞬间误报。"""
        now_mono = time.monotonic() if now_mono is None else now_mono
        downs = [t for t in self.down_mono.values() if t is not None]
        if downs and not self.alerted and now_mono - min(downs) > ALERT_AFTER:
            self.alerted = True
            return int((now_mono - min(downs)) / 60)
        return None

    def mark_down(self, name: str):
        if self.down_mono.get(name) is None:
            self.down_mono[name] = time.monotonic()

    def mark_up(self, name: str):
        t0 = self.down_mono.get(name)
        self.down_mono[name] = None
        if t0 is not None:
            mins = (time.monotonic() - t0) / 60
            log(f"[{name}] 断线已恢复（持续约 {mins:.1f} 分钟）" + ("" if self.alerted else "，未超过阈值，仅记录本地日志不通知"))
        if not any(v is not None for v in self.down_mono.values()):
            self.alerted = False

    async def _check_alert(self):
        while True:
            await asyncio.sleep(30)
            mins = self.alert_due()
            if mins is not None:
                log(f"告警：WebSocket 已连续断线 {mins} 分钟仍未恢复 → 通知一次")
                await asyncio.to_thread(notify, {"kind": "disconnect_alert", "message": f"OKX 模拟盘 WebSocket 已连续断线 {mins} 分钟仍未恢复"})

    async def session(self, name: str):
        import websockets
        delay = 1
        while True:
            try:
                async with websockets.connect(URLS[name], ping_interval=None, open_timeout=15, close_timeout=5) as ws:
                    await ws.send(json.dumps(login_msg()))
                    r = json.loads(await asyncio.wait_for(ws.recv(), 15))
                    if r.get("event") != "login" or str(r.get("code")) != "0":
                        raise RuntimeError(f"登录失败 code={r.get('code')} msg={r.get('msg')}")
                    log(f"[{name}] 登录成功 connId={r.get('connId')}")
                    await ws.send(json.dumps({"id": f"{name}1", "op": "subscribe", "args": SUBS[name]}))
                    reconnect = name in self.ever_connected
                    self.ever_connected.add(name)
                    if self.down_since[name]:
                        try:
                            ws_down_mark(name, end_ms=_now_ms())
                        except Exception as e:
                            log(f"记录断线区间失败 {type(e).__name__}")
                    self.down_since[name] = None
                    self.mark_up(name)
                    self.last_alive[name] = time.time()
                    delay = 1
                    if reconnect and name == "private":
                        self._catchup()
                    await self._loop(name, ws)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log(f"[{name}] 断线/异常：{type(e).__name__}: {e}\n{traceback.format_exc()}")
            self.mark_down(name)
            if not self.down_since[name]:
                self.down_since[name] = self.last_alive[name] or time.time()
                try:
                    ws_down_mark(name, start_ms=int(self.down_since[name] * 1000))
                except Exception as e:
                    log(f"记录断线区间失败 {type(e).__name__}")
            wait = min(BACKOFF_MAX, delay) * (0.8 + 0.4 * random.random())
            log(f"[{name}] {wait:.1f}s 后重连")
            await asyncio.sleep(wait)
            delay = min(BACKOFF_MAX, delay * 2)

    async def _loop(self, name, ws):
        last_ping = 0.0
        pong_wait = None
        while True:
            now = time.time()
            if now - last_ping >= PING_EVERY:
                await ws.send("ping"); last_ping = now; pong_wait = pong_wait or now
            if pong_wait and now - pong_wait > 20:
                raise RuntimeError("心跳超时（20s 未收到 pong）")
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                continue
            self.last_alive[name] = time.time()
            if raw == "pong":
                pong_wait = None; beat(); continue
            msg = json.loads(raw)
            if not isinstance(msg, dict):   # 非对象推送（例如纯字符串）不按字典处理，避免 'str'.get 断线
                log(f"[{name}] 非对象推送已忽略：{str(raw)[:200]}"); continue
            if msg.get("event") == "subscribe":
                log(f"[{name}] 订阅成功 {json.dumps(msg.get('arg'), ensure_ascii=False)}")
            elif msg.get("event") == "error":
                log(f"[{name}] 订阅/请求错误 code={msg.get('code')} msg={msg.get('msg')}")
            elif msg.get("event") in ("notice", "channel-conn-count"):
                log(f"[{name}] {msg.get('event')} {json.dumps(msg, ensure_ascii=False)[:300]}")
            elif "data" in msg:
                try:   # 单条推送处理失败只记录完整堆栈与推送摘要，不断开连接
                    for ev in parse(msg):
                        await asyncio.to_thread(handle, ev, self.st)
                except Exception as e:
                    log(f"[{name}] 推送处理失败 {type(e).__name__}: {e} 推送={json.dumps(msg, ensure_ascii=False)[:500]}\n{traceback.format_exc()}")

    async def main(self):
        check_code_version()
        log(f"启动 pid={os.getpid()} 代码版本={str(_code_rev['start'])[:7]} 地址={list(URLS.values())}")
        await asyncio.gather(*(self.session(n) for n in URLS), self._check_alert(), self._risk_review(), self._entry_timer())

    async def _entry_timer(self):
        """R6b：限价入场超时由 demo_ws 定时检查，不再只靠 demo_watch 轮询（最坏延迟从约 10 分钟降到 ws_check_secs）。"""
        while True:
            secs = entry_timer_secs()
            await asyncio.sleep(secs or 60)
            if not secs:
                continue
            try:
                res = await asyncio.to_thread(entry_limit_tick, settings.load_config())
                for aid, r in res:
                    if r not in ("armed", "waiting"):
                        log(f"限价入场 {aid}: {r}")
            except Exception as e:
                log(f"限价入场定时检查失败 {type(e).__name__}: {e}")

    async def _risk_review(self):
        """R10：每 REVIEW_EVERY 秒检查 KILL/HALT/当日亏损/回撤，触发即撤入场类挂单（desk/trigger_review.py）。"""
        while True:
            await asyncio.sleep(REVIEW_EVERY)
            try:
                from desk import trigger_review
                rv = await asyncio.to_thread(trigger_review.poll, settings.load_config())
                if rv.get("reasons") and (rv.get("algos") or rv.get("orders") or rv.get("errors")):
                    log(f"风险停机 {rv['reasons']}：撤入场条件单 {rv.get('algos')}、入场子单 {rv.get('orders')}，失败 {rv.get('errors')}")
            except Exception as e:
                log(f"风险复核失败 {type(e).__name__}: {e}")


def entry_timer_secs(cfg=None) -> int:
    from desk import demo_exec as DX
    try:
        el = DX.ecfg(cfg or settings.load_config())["entry_limit"]
        return int(float(el.get("ws_check_secs") or 0)) if el.get("enabled") else 0
    except Exception:
        return 0


def entry_limit_tick(cfg) -> list:
    """R6b 定时检查一次：HALT 生效或没有未完结的限价入场时不调用（不发任何请求）。"""
    from desk import entry_limit
    if settings.halt_active():
        return []
    s = entry_limit._load()
    if not any(e.get("status") != "done" for e in s.get("entries", {}).values()):
        return []
    return entry_limit.check_timeouts(cfg)


def main():
    lockf = open(settings.STATE / "demo_ws.lock", "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("已有 demo_ws 实例在运行，退出"); return 1
    try:
        asyncio.run(Runner().main())
    except Exception:
        log("致命错误\n" + traceback.format_exc()); return 1


if __name__ == "__main__":
    raise SystemExit(main())
