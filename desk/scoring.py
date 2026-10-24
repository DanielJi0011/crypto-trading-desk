"""角色打分：每个角色每次发言的观点在 horizon_days 天后用 OKX 日线收盘价打分。

- 方向命中：未来 N 天涨跌幅 > +带宽 记为"涨"，< -带宽 记为"跌"，否则"平"；看多→涨、看空→跌、中性→平 记为命中。
- 止损质量：看多且给了止损价：期间最低价触及止损 → 若最终仍下跌记"止损有效"，若最终收涨记"被洗出"；未触及记"未触及"。
- 盈亏归因：已平仓交易的盈亏 × 该角色当时方向（看多+1/看空-1/中性0/平仓0）。
- 平仓/减仓（direction="close"，交易经理对已有持仓 sell/reduce）：是仓位管理动作，不是方向预测，不参与方向命中打分。
- 市况：决策时该币 20 日涨跌幅 ≥+阈值 上涨 / ≤-阈值 下跌 / 其余 震荡。

第三批 L2（打分 v2，scoring_v=2）：
- 周期：每条观点按自己的 horizon_days（夹到 1~30 天，缺省用 scoring.horizon_days）打分。
- 条件入场观点（entry_condition=gte/lte 且有 entry_px）：从触发当天起算，参考价=entry_px；周期内未触发记 not_triggered，不计命中。
- 中性带宽：0.5×ATR14%×√h（ATR14 用决策日之前已收盘日线）；ATR 不可得时退回 neutral_band_pct。
- 基础率 base_rate：决策日之前 365 天内所有 h 天窗口里，结果恰为该观点所猜方向的比例（同一带宽）；skill = hit − base_rate。
  例：单边上涨里永远看多的角色 hit 高、base_rate 也高，skill≈0，不因此加权。
- Brier：预测概率 p = 1/3 + 2/3×置信度，brier = (p − hit)²；成绩单按置信度分桶。
- 自带方案的 R 倍数：观点有止损（及可选目标）时，按日线路径模拟（同一根先算止损，保守），无止损记 no_plan。
- 影子权重：w = w0·clip(exp(k·Σskill/(n+n0)), 0.5, 2)，同一角色同一天多个币先取平均算一个样本。"""
from __future__ import annotations

import datetime as _dt
import math

import pandas as pd

from desk import okx_data, settings
from desk.roles import INIT_WEIGHT, ROLE_KEYS

STANCES = settings.STATE / "stances.jsonl"
SCORES = settings.STATE / "scores.jsonl"
WEIGHTS = settings.STATE / "weights.json"
WEIGHTS_SHADOW = settings.STATE / "weights_shadow.json"   # 学习冻结时的影子权重（L1）
WEIGHT_HIST = settings.STATE / "weights_history.jsonl"


def regime_of(ret20: float, cfg) -> str:
    t = cfg["scoring"]["regime_threshold_pct"]
    return "上涨" if ret20 >= t else ("下跌" if ret20 <= -t else "震荡")


def record_stances(cfg, rec: dict):
    for r in list(ROLE_KEYS) + (["trade_manager"] if "trade_manager" in rec["stances"] else []):
        s = rec["stances"][r]
        settings.append_jsonl(STANCES, {
            "id": f"{rec['decision_id']}:{r}", "decision_id": rec["decision_id"], "date": rec["date"], "time": rec["time"],
            "session": rec.get("session", "morning"),
            "inst_id": rec["inst_id"], "role": r, "direction": s["direction"], "confidence": s.get("confidence"),
            "target": s.get("target"), "stop": s.get("stop"), "horizon_days": s.get("horizon_days"),
            "entry_px": s.get("entry_px"), "entry_condition": s.get("entry_condition"), "source_path": rec.get("source", "daily"),
            "ref_px": rec["price"], "regime": regime_of(rec["market"]["ret_20d"], cfg), "source": s.get("source")})


DIR_OUTCOME = {"bullish": "涨", "bearish": "跌", "neutral": "平"}   # 只有这三种方向参与打分；"close" 不打分


def DAILY(inst):   # 可在测试中替换
    return okx_data.candles(inst, "1Dutc", 420)


def HOURLY(inst):   # 晚场打分用 1H 已收盘K线（约 62 天），可在测试中替换
    return okx_data.candles(inst, "1H", 1500)


def _t0_utc(s) -> pd.Timestamp:
    """观点时刻（stances.time，北京时区字符串）→ naive UTC，向上取整到整点（只用决策之后开盘的 1H K线）。"""
    t = _dt.datetime.strptime(s["time"], "%Y-%m-%d %H:%M:%S%z").astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return pd.Timestamp(t).ceil("h")


def _score_evening(cfg, s, h, now, hcache, dcache):
    """晚场观点：规则与早场相同（同一带宽、基准率、skill、止损/目标/R 倍数），只是持有期从晚场决策时刻起算，
    路径用决策之后的 1H 已收盘K线，持有 h×24 小时；条件单在决策后 h×24 小时内触发，触发后再看 h×24 小时。返回 None = 未到期。"""
    inst = s["inst_id"]
    if inst not in hcache:
        hcache[inst] = HOURLY(inst)
    if inst not in dcache:
        dcache[inst] = DAILY(inst)
    hb, d = hcache[inst], dcache[inst]
    d0 = _dt.date.fromisoformat(s["date"])
    t0, H = _t0_utc(s), pd.Timedelta(hours=24 * h)
    now_u = pd.Timestamp(now.astimezone(_dt.timezone.utc).replace(tzinfo=None))
    ref, start, trig = float(s["ref_px"]), t0, None
    cond, epx = s.get("entry_condition"), s.get("entry_px")
    base = {"id": s["id"], "decision_id": s["decision_id"], "role": s["role"], "inst_id": inst, "date": s["date"],
            "time": s["time"], "session": "evening", "regime": s["regime"], "direction": s["direction"],
            "confidence": s.get("confidence"), "horizon": h, "scoring_v": 2}
    if cond in ("gte", "lte") and epx:
        epx = float(epx)
        if now_u < t0 + H:
            return None
        win = hb[(hb.Date >= t0) & (hb.Date < t0 + H)]
        if win.empty or win.Date.iloc[-1] < t0 + H - pd.Timedelta(hours=1):
            return None
        hit = win[win.High >= epx] if cond == "gte" else win[win.Low <= epx]
        if hit.empty:
            return {**base, "outcome": "not_triggered", "hit": None, "skill": None, "scored_at": settings.now_str()}
        start, ref = hit.Date.iloc[0], epx
        trig = str(start)
    if now_u < start + H:
        return None
    path = hb[(hb.Date >= start) & (hb.Date < start + H)]
    if path.empty or path.Date.iloc[-1] < start + H - pd.Timedelta(hours=1):
        return None
    band, band_src = band_for(cfg, d, d0, h)
    ret = (float(path.Close.iloc[-1]) / ref - 1) * 100
    outcome = outcome_of(ret, band)
    want = DIR_OUTCOME[s["direction"]]
    hit = want == outcome
    br = base_rate(d, d0, h, band, want)
    conf = s.get("confidence")
    try:
        pr = 1 / 3 + 2 / 3 * min(max(float(conf), 0.0), 1.0)
    except (TypeError, ValueError):
        pr = 1 / 3
    stop_q = None
    if s["direction"] == "bullish" and s.get("stop") and s["stop"] < ref:
        touched = float(path.Low.min()) <= s["stop"]
        stop_q = ("止损有效" if ret < 0 else "被洗出") if touched else "未触及"
    elif s["direction"] == "bearish" and s.get("stop") and s["stop"] > ref:
        touched = float(path.High.max()) >= s["stop"]
        stop_q = ("止损有效" if ret > 0 else "被洗出") if touched else "未触及"
    tgt_hit = None
    if s.get("target"):
        tgt_hit = bool(float(path.High.max()) >= s["target"]) if s["target"] > ref else bool(float(path.Low.min()) <= s["target"])
    r_mult, r_exit = plan_r(s, path, ref)
    return {**base, "ret_pct": round(ret, 3), "outcome": outcome, "hit": hit, "stop_quality": stop_q, "target_hit": tgt_hit,
            "band_pct": round(band, 3), "band_src": band_src, "triggered": trig,
            "base_rate": None if br is None else round(br, 4), "skill": round(float(hit) - (br if br is not None else 1 / 3), 4),
            "brier": round((pr - float(hit)) ** 2, 4), "plan_r": r_mult, "plan_exit": r_exit, "scored_at": settings.now_str()}


def _atr_pct(d, d0) -> float | None:
    """决策日之前（不含当日）最近 14 根日线的 ATR14 占收盘价百分比。"""
    pre = d[d.Date < pd.Timestamp(d0)].tail(15)
    if len(pre) < 15:
        return None
    hi, lo, cl = pre.High.values, pre.Low.values, pre.Close.values
    tr = [max(hi[i] - lo[i], abs(hi[i] - cl[i - 1]), abs(lo[i] - cl[i - 1])) for i in range(1, len(pre))]
    return sum(tr) / len(tr) / float(cl[-1]) * 100


def band_for(cfg, d, d0, h) -> tuple[float, str]:
    a = _atr_pct(d, d0)
    if a is None:
        return float(cfg["scoring"]["neutral_band_pct"]), "fixed"
    return 0.5 * a * math.sqrt(h), "atr"


def outcome_of(ret: float, band: float) -> str:
    return "涨" if ret > band else ("跌" if ret < -band else "平")


def base_rate(d, d0, h, band, want: str, lookback=365, min_n=30) -> float | None:
    """决策日之前 lookback 天内、窗口终点早于 d0 的所有 h 天窗口中结果等于 want 的比例。样本不足 → None。"""
    pre = d[(d.Date < pd.Timestamp(d0)) & (d.Date >= pd.Timestamp(d0) - pd.Timedelta(days=lookback + h))].reset_index(drop=True)
    cl = pre.Close.values
    outs = [outcome_of((cl[i + h] / cl[i] - 1) * 100, band) for i in range(0, len(cl) - h)]
    if len(outs) < min_n:
        return None
    return sum(1 for o in outs if o == want) / len(outs)


def plan_r(s: dict, path, ref: float) -> tuple[float | None, str]:
    """L2(b)：角色自带方案的 R 倍数。无止损 → (None, no_plan)。"""
    stop, tgt, dr = s.get("stop"), s.get("target"), s.get("direction")
    if dr not in ("bullish", "bearish") or not stop:
        return None, "no_plan"
    sgn = 1 if dr == "bullish" else -1
    risk = (ref - float(stop)) * sgn
    if risk <= 0:
        return None, "no_plan"
    reward = (float(tgt) - ref) * sgn if tgt else None
    if reward is not None and reward <= 0:
        reward = None
    for _, b in path.iterrows():
        hit_stop = (b.Low <= stop) if sgn > 0 else (b.High >= stop)
        if hit_stop:
            return -1.0, "stop"
        if reward is not None and ((b.High >= tgt) if sgn > 0 else (b.Low <= tgt)):
            return round(reward / risk, 3), "target"
    return round((float(path.Close.iloc[-1]) - ref) * sgn / risk, 3), "time"


RULE_ID = __import__("re").compile(r"H-\d{8}-[a-z_]+-[0-9a-f]{6}")


def tag_rules(rec: dict, stance: dict) -> dict:
    """S2：记录该观点引用了哪些研究规则（理由文本里出现的 H-日期-角色-哈希 编号），便于区分规则驱动与自由判断的成绩。"""
    txt = " ".join(str(stance.get(k) or "") for k in ("reason", "rationale", "cited_rules"))
    rec["rules_cited"] = sorted(set(RULE_ID.findall(txt)))
    return rec


def score_due(cfg, now=None) -> list[dict]:
    """L2 打分 v2（见模块说明）。"""
    now = now or settings.now()
    h0 = int(cfg["scoring"]["horizon_days"])
    done = {s["id"] for s in settings.read_jsonl(SCORES)}
    new, cache, hcache = [], {}, {}
    for s in settings.read_jsonl(STANCES):
        if s["id"] in done or s.get("direction") not in DIR_OUTCOME:   # "close"（平仓/减仓）不计入方向打分
            continue
        from desk.stances import INVALID_STATUS, stance_status
        if stance_status(s) in INVALID_STATUS:   # L3：抽取失败/无发言/数据缺失不是观点，不打分
            continue
        try:
            h = int(s.get("horizon_days") or h0)
        except (TypeError, ValueError):
            h = h0
        h = min(max(h, 1), 30)
        if (s.get("session") or "morning") == "evening":   # 晚场：独立样本，持有期从晚场决策时刻起算
            r = _score_evening(cfg, s, h, now, hcache, cache)
            if r is not None:
                settings.append_jsonl(SCORES, tag_rules(r, s)); new.append(r)
            continue
        d0 = _dt.date.fromisoformat(s["date"])
        cond, epx = s.get("entry_condition"), s.get("entry_px")
        conditional = cond in ("gte", "lte") and epx
        # 最晚需要的日线：条件单最多等 h 天触发，再看 h 天
        if now < _dt.datetime.combine(d0 + _dt.timedelta(days=h), _dt.time(8, 0), settings.TZ):
            continue
        if s["inst_id"] not in cache:
            cache[s["inst_id"]] = DAILY(s["inst_id"])
        d = cache[s["inst_id"]]
        ref, start = float(s["ref_px"]), d0
        trig_date = None
        if conditional:
            epx = float(epx)
            win = d[(d.Date >= pd.Timestamp(d0)) & (d.Date <= pd.Timestamp(d0 + _dt.timedelta(days=h - 1)))]
            if len(win) < h:
                continue
            hitbar = win[(win.High >= epx)] if cond == "gte" else win[(win.Low <= epx)]
            if hitbar.empty:
                rec = {"id": s["id"], "decision_id": s["decision_id"], "role": s["role"], "inst_id": s["inst_id"],
                       "date": s["date"], "time": s["time"], "regime": s["regime"], "direction": s["direction"],
                       "confidence": s.get("confidence"), "horizon": h, "outcome": "not_triggered", "hit": None,
                       "skill": None, "scoring_v": 2, "scored_at": settings.now_str()}
                settings.append_jsonl(SCORES, tag_rules(rec, s)); new.append(rec)
                continue
            start = hitbar.Date.iloc[0].date(); ref = epx; trig_date = str(start)
        last_bar = start + _dt.timedelta(days=h - 1)       # 该 UTC 日线在 start+h 日 08:00（北京）收盘
        if now < _dt.datetime.combine(start + _dt.timedelta(days=h), _dt.time(8, 0), settings.TZ):
            continue
        path = d[(d.Date >= pd.Timestamp(start)) & (d.Date <= pd.Timestamp(last_bar))]
        if len(path) < h:
            continue
        band, band_src = band_for(cfg, d, d0, h)
        p_end = float(path.Close.iloc[-1])
        ret = (p_end / ref - 1) * 100
        outcome = outcome_of(ret, band)
        want = DIR_OUTCOME[s["direction"]]
        hit = want == outcome
        br = base_rate(d, d0, h, band, want)
        skill = round(float(hit) - (br if br is not None else 1 / 3), 4)
        conf = s.get("confidence")
        try:
            pr = 1 / 3 + 2 / 3 * min(max(float(conf), 0.0), 1.0)
        except (TypeError, ValueError):
            pr = 1 / 3
        stop_q = None
        if s["direction"] == "bullish" and s.get("stop") and s["stop"] < ref:
            touched = float(path.Low.min()) <= s["stop"]
            stop_q = ("止损有效" if ret < 0 else "被洗出") if touched else "未触及"
        elif s["direction"] == "bearish" and s.get("stop") and s["stop"] > ref:   # 空头观点：止损在上方
            touched = float(path.High.max()) >= s["stop"]
            stop_q = ("止损有效" if ret > 0 else "被洗出") if touched else "未触及"
        tgt_hit = None
        if s.get("target"):
            tgt_hit = bool(float(path.High.max()) >= s["target"]) if s["target"] > ref else bool(float(path.Low.min()) <= s["target"])
        r_mult, r_exit = plan_r(s, path, ref)
        rec = {"id": s["id"], "decision_id": s["decision_id"], "role": s["role"], "inst_id": s["inst_id"], "date": s["date"],
               "time": s["time"], "regime": s["regime"], "direction": s["direction"], "confidence": conf,
               "ret_pct": round(ret, 3), "outcome": outcome, "hit": hit, "stop_quality": stop_q, "target_hit": tgt_hit,
               "horizon": h, "band_pct": round(band, 3), "band_src": band_src, "triggered": trig_date,
               "base_rate": None if br is None else round(br, 4), "skill": skill, "brier": round((pr - float(hit)) ** 2, 4),
               "plan_r": r_mult, "plan_exit": r_exit, "scoring_v": 2, "scored_at": settings.now_str()}
        settings.append_jsonl(SCORES, tag_rules(rec, s))
        new.append(rec)
    return new


def scores(role=None, include_untriggered=False) -> list[dict]:
    """默认只返回有命中判定的打分（L2：not_triggered 的 hit=None，不参与命中率/修订/权重）。"""
    out = [s for s in settings.read_jsonl(SCORES) if (role is None or s["role"] == role)
           and (include_untriggered or s.get("hit") is not None)]
    return sorted(out, key=lambda s: s["time"])


def direction_alignment(trades: list[dict], stances_by_decision: dict) -> dict:
    keys = list(ROLE_KEYS) + ["trade_manager"]
    att = {r: 0.0 for r in keys}
    dn = {"bullish": 1, "bearish": -1, "neutral": 0, "close": 0}
    for t in trades:
        if t.get("status") != "closed":
            continue
        st = stances_by_decision.get(t.get("signal_id"), {})
        for r in keys:
            if r in st:
                sgn = -1 if t.get("side") == "sell" else 1   # 空单盈利 → 看空角色得正归因
                att[r] += dn[st[r]["direction"]] * sgn * float(t.get("pnl_usd") or 0)
    return att


pnl_attribution = direction_alignment   # 旧名保留兼容：这是"方向与交易盈亏同向"的对齐度，不是因果归因（L2(c)）


def scorecard(role: str, trades=None, stances_by_decision=None) -> dict:
    ss = scores(role)   # not_triggered 不计命中
    def agg(xs):
        n = len(xs)
        return {"n": n, "hit": (sum(x["hit"] for x in xs) / n) if n else None}

    def mean(xs):
        xs = [x for x in xs if x is not None]
        return round(sum(xs) / len(xs), 4) if xs else None
    card = {"role": role, **agg(ss), "recent": agg(ss[-20:]),
            "by_regime": {g: agg([x for x in ss if x["regime"] == g]) for g in ("上涨", "下跌", "震荡")},
            "by_direction": {d: agg([x for x in ss if x["direction"] == d]) for d in ("bullish", "bearish", "neutral")}}
    sq = [x["stop_quality"] for x in ss if x.get("stop_quality")]
    card["stops"] = {k: sq.count(k) for k in ("止损有效", "被洗出", "未触及")}
    v2 = [x for x in ss if x.get("scoring_v") == 2]
    card["skill"] = mean([x.get("skill") for x in v2])
    card["brier"] = mean([x.get("brier") for x in v2])
    card["conf_buckets"] = {k: {**agg(xs), "brier": mean([x.get("brier") for x in xs])} for k, xs in (
        ("<0.4", [x for x in v2 if (x.get("confidence") or 0) < 0.4]),
        ("0.4~0.7", [x for x in v2 if 0.4 <= (x.get("confidence") or 0) < 0.7]),
        ("≥0.7", [x for x in v2 if (x.get("confidence") or 0) >= 0.7]))}
    pr = [x["plan_r"] for x in v2 if x.get("plan_r") is not None]
    card["plan_r"] = {"n": len(pr), "mean": mean(pr), "no_plan": sum(1 for x in v2 if x.get("plan_exit") == "no_plan")}
    card["not_triggered"] = sum(1 for x in scores(role, include_untriggered=True) if x.get("outcome") == "not_triggered")
    if trades is not None:
        card["pnl_attr"] = direction_alignment(trades, stances_by_decision or {})[role]
    return card


def weight_roles() -> dict:
    """参与权重调整的角色及初始权重：ROLE_KEYS 去掉组合经理（只执行、不投票），加上交易经理（TM_INIT_WEIGHT）。"""
    from desk.manager import TM_INIT_WEIGHT
    out = {r: INIT_WEIGHT[r] for r in ROLE_KEYS if r != "portfolio_manager"}
    out["trade_manager"] = TM_INIT_WEIGHT
    return out


def daily_skill(role: str) -> list[float]:
    """L2：同一角色同一天同一场（多个币）的 skill 先平均，作为一个样本；早场与晚场各算独立样本。只用 v2 打分。"""
    by = {}
    for x in scores(role):
        if x.get("scoring_v") == 2 and x.get("skill") is not None:
            by.setdefault((x["date"], x.get("session") or "morning"), []).append(float(x["skill"]))
    return [sum(v) / len(v) for _, v in sorted(by.items())]


def update_weights(cfg) -> list[dict]:
    """L2 收缩权重：w = w0·clip(exp(k·Σskill/(n+n0)), lo, hi)。学习冻结时只写影子权重。"""
    L = cfg["learning"]
    init = weight_roles()
    target = WEIGHTS if L.get("apply_weights", False) else WEIGHTS_SHADOW   # 学习冻结：只更新影子权重
    old = settings.read_json(target, dict(init))
    new, changes = {}, []
    lo, hi = L["weight_clip"]
    sc = cfg["scoring"]
    n0, k = float(sc.get("n0", 30)), float(sc.get("k", 4))
    for r, w0 in init.items():
        ds = daily_skill(r)
        n = len(ds)
        new[r] = round(w0 * min(max(math.exp(k * sum(ds) / (n + n0)), lo), hi), 4) if n else w0
        ss = ds
        if abs(new[r] - float(old.get(r, w0))) > 1e-6:
            changes.append({"role": r, "old": float(old.get(r, w0)), "new": new[r], "n": len(ss)})
    settings.write_json(target, new)   # portfolio_manager 不再写入
    if changes:
        settings.append_jsonl(WEIGHT_HIST, {"time": settings.now_str(), "changes": changes, "shadow": target == WEIGHTS_SHADOW})
    return changes
