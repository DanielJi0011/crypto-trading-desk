"""每周自研：分析师提出可检验的做多/做空假设（side=long|short；缺失或非法记为 unknown「未知」，不猜方向、不检验、不写手册） → 预注册（写入 research/hypotheses.jsonl，检验前不可改）→
在 OKX 日线历史上做样本内/样本外检验（复用 ai-trader/backtest.py 的 simulate）→ 通过多重检验门槛的规则才写入该角色手册
（版本化、每次最多 2 处（多空合计）、修订后观察期与自动回滚规则同复盘修订）。

多空对称口径：设 gross = c[t+h]/o[t+1] − 1（无条件同入场/同持有期），fee 为单边费用。
  做多：交易净收益(已扣 2·fee) − (gross 均值 − 2·fee)
  做空：交易净收益(已扣 2·fee) − (−gross 均值 − 2·fee)，即与同期「自然跌幅」相比
两边基准都扣同样的双边费用，费用在超额里相互抵消；多空同一口径。Bonferroni 累计检验数按角色计、多空合并，只计实际做过检验的假设（含旧版已检验的记录；unknown 跳过的不计）。"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from statistics import NormalDist
from typing import Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from desk import roles, scoring, settings
from desk import llm as dllm
from desk.roles import ROLE_CN

RDIR = settings.ROOT / "research"
REGISTRY = RDIR / "hypotheses.jsonl"
RESULTS = RDIR / "results.jsonl"
ANNOT = RDIR / "annotations.jsonl"          # S2：对已登记假设的事后标注（登记表本身不改）
REJECTED = RDIR / "rejected.jsonl"          # S2：菜单外特征的假设，检验前拒绝，不登记、不计入检验数
# S2：特征与角色职能的关联说明（菜单受限于日线价量数据，部分角色没有与职能直接相关的特征）
ROLE_FEATURE_NOTES = {
    "news": "新闻分析师只有成交量 Z 值一个特征，属于价量代理，并不检验新闻内容本身；通过的规则不代表新闻判断能力。",
    "sentiment": "情绪分析师的特征是 RSI 与距高低点幅度，属于价格派生的情绪代理，不含社媒数据。",
    "flows": "资金流分析师的特征只有成交量与当日涨跌，不含持仓量、资金费率、多空比等真正的资金流数据。",
}
LAST = RDIR / "last_run.json"

FEATURES = {
    "ret_1": "当日涨跌幅%", "ret_3": "3日涨跌幅%", "ret_5": "5日涨跌幅%", "ret_20": "20日涨跌幅%",
    "rsi_14": "RSI(14)", "dist_ma20": "收盘相对20日均线偏离%", "dist_ma50": "收盘相对50日均线偏离%",
    "atr_pct": "ATR14/收盘%", "vol_z20": "成交量20日Z值", "range_pct": "当日振幅%",
    "dd_high20": "距20日最高价回撤%（≤0）", "up_high20": "距20日最低价涨幅%（≥0）",
    "btc_ret_5": "BTC 5日涨跌幅%（市场整体）", "dow": "星期（0=周一…6=周日，UTC日线）",
    "vol_dir": "量价方向 = vol_z20 × sign(当日涨跌)（正=量增价涨，负=量增价跌）",
}


def role_features(cfg, role) -> list[str]:
    """该角色可用的特征（config.yaml research.role_features；未配置则全菜单）。"""
    rf = (cfg.get("research", {}).get("role_features") or {}).get(role)
    return [k for k in (rf or FEATURES) if k in FEATURES]


def features(df: pd.DataFrame, btc: pd.DataFrame | None = None) -> pd.DataFrame:
    c, h, l, v = df.c, df.h, df.l, df.v
    f = pd.DataFrame(index=df.index)
    for n in (1, 3, 5, 20):
        f[f"ret_{n}"] = (c / c.shift(n) - 1) * 100
    d = c.diff()
    up, dn = d.clip(lower=0).ewm(alpha=1 / 14).mean(), (-d.clip(upper=0)).ewm(alpha=1 / 14).mean()
    f["rsi_14"] = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    f["dist_ma20"] = (c / c.rolling(20).mean() - 1) * 100
    f["dist_ma50"] = (c / c.rolling(50).mean() - 1) * 100
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    f["atr_pct"] = tr.rolling(14).mean() / c * 100
    f["vol_z20"] = (v - v.rolling(20).mean()) / v.rolling(20).std()
    f["range_pct"] = (h - l) / c * 100
    f["dd_high20"] = (c / h.rolling(20).max() - 1) * 100
    f["up_high20"] = (c / l.rolling(20).min() - 1) * 100
    if btc is not None:
        b = btc.set_index("time").c
        f["btc_ret_5"] = df.time.map((b / b.shift(5) - 1) * 100).values
    else:
        f["btc_ret_5"] = f["ret_5"]
    f["dow"] = pd.to_datetime(df.time).dt.dayofweek
    f["vol_dir"] = f["vol_z20"] * np.sign(f["ret_1"])
    return f


class Cond(BaseModel):
    feature: str = Field(description="特征名，只能从给定菜单里选")
    op: Literal["<", ">"]
    value: float


class Hypo(BaseModel):
    side: str = Field(default="unknown", description="必填：long=做多，short=做空")
    name: str = Field(description="简短中文名")
    rationale: str = Field(description="从你的角色视角，为什么这条规则可能有效（≤80字）")
    conditions: list[Cond] = Field(description="1~3 个条件，全部满足时在下一根日线开盘按 side 方向入场")
    horizon_days: Literal[1, 3, 5, 10]
    stop_pct: float = Field(default=8.0, description="止损%（2~15）")


class HypoList(BaseModel):
    hypotheses: list[Hypo]


def _signal(f: pd.DataFrame, conds: list) -> pd.Series:
    s = pd.Series(True, index=f.index)
    for c in conds:
        col = f[c["feature"]]
        s &= (col < c["value"]) if c["op"] == "<" else (col > c["value"])
    return s.fillna(False)


def _t(x):
    x = np.asarray(x, float)
    if len(x) < 3 or x.std(ddof=1) == 0:
        return 0.0
    return float(x.mean() / x.std(ddof=1) * math.sqrt(len(x)))


def nw_t(x, lag: int) -> float:
    """S2：Newey-West（Bartlett 核）t 值，lag = 持有期；重叠持有期造成的自相关会让普通 t 偏大。"""
    x = np.asarray(x, float)
    n = len(x)
    if n < 3:
        return 0.0
    e = x - x.mean()
    v = float(e @ e) / n
    for k in range(1, min(int(lag), n - 1) + 1):
        v += 2 * (1 - k / (lag + 1)) * float(e[k:] @ e[:-k]) / n
    if v <= 0:
        return 0.0
    return float(x.mean() / math.sqrt(v / n))


def per_day(rows) -> list:
    """S2：同一入场日（多个币同时触发）合并为一个观测（取均值），避免把同一天的共同行情当作独立样本。"""
    by = {}
    for d, r in rows:
        by.setdefault(str(d)[:10], []).append(r)
    return [float(np.mean(by[k])) for k in sorted(by)]


def annotations() -> dict:
    out = {}
    for a in settings.read_jsonl(ANNOT):
        out.setdefault(a["id"], {}).update({k: v for k, v in a.items() if k not in ("id", "time")})
    return out


def global_k(extra: int = 1) -> int:
    """S2：多重检验数按全局累计（所有角色、多空合并、只计实际检验过的；菜单违规的历史假设也计入）。"""
    done = {x["id"] for x in settings.read_jsonl(RESULTS) if not x.get("skipped")}
    return len(done) + extra


def side_of(h: dict) -> str:
    """只认 long/short；缺失或其他取值一律 unknown，不猜方向。"""
    v = str(h.get("side") or "").strip().lower()
    return v if v in ("long", "short") else "unknown"


SIDE_CN = {"long": "做多", "short": "做空", "unknown": "未知"}


def _skipped(h: dict) -> dict:
    return {"side": "unknown", "skipped": True, "passed": False, "why": "未知方向，跳过检验",
            "stats": {p: {"n": 0, "mean_excess_pct": None, "t": None} for p in ("is", "oos")}, "z_crit_is": None, "k_tests": None}


def evaluate(h: dict, data: dict, cfg, k_tests: int) -> dict:
    """data: {inst: df(time,o,h,l,c,v)}；三个币汇总。超额收益 = 交易收益 − 同方向无条件基准（做多：漂移−双边费；做空：−漂移−双边费）。"""
    from desk.tools import ai_backtest
    bt = ai_backtest()
    side = side_of(h)
    if side == "unknown":
        return _skipped(h)
    R = cfg["research"]
    btc = data.get("BTC-USDT") if "BTC-USDT" in data else data.get("BTC-USDT-SWAP")
    times = sorted(set(pd.concat([d.time for d in data.values()])))
    cut = times[int(len(times) * (1 - R["oos_frac"]))]
    out = {"is": [], "oos": []}
    for inst, df in data.items():
        f = features(df, btc)
        sig = _signal(f, h["conditions"])
        hz = h["horizon_days"]
        gross = df.c.shift(-hz) / df.o.shift(-1) - 1
        sign = 1 if side == "long" else -1
        fwd = sign * gross - 2 * R["fee"]           # 同方向无条件基准（同样的入场/持有/双边费用）
        for part, mask in (("is", df.time < cut), ("oos", df.time >= cut)):
            sub = df[mask].reset_index(drop=True)
            if len(sub) < 60:
                continue
            base = float(fwd[mask].dropna().mean())
            res = bt.simulate(sub, sig[mask].reset_index(drop=True), side=side, sl_pct=float(h.get("stop_pct", 8)),
                              max_bars=hz, fee=R["fee"])
            out[part] += [(t["entry_time"], t["ret_pct"] / 100 - base) for t in res["trades"]]
    phase = R.get("phase", "explore")
    # 探索期：单次检验名义门槛，通过只算“探索通过”；确认期（每季度）才按全局累计数做 Bonferroni
    zcrit = NormalDist().inv_cdf(1 - R["alpha"] / (max(k_tests, 1) if phase == "confirm" else 1))
    s = {}
    for p, x in out.items():
        days = per_day(x)
        s[p] = {"n": len(x), "n_days": len(days), "mean_excess_pct": round(float(np.mean(days)) * 100, 3) if days else None,
                "t": round(nw_t(days, h["horizon_days"]), 2), "t_naive": round(_t([r for _, r in x]), 2)}
    ok = (s["is"]["n_days"] >= R["min_trades_is"] and s["is"]["t"] >= zcrit and s["oos"]["n_days"] >= R["min_trades_oos"]
          and s["oos"]["t"] >= R["min_t_oos"] and (s["oos"]["mean_excess_pct"] or 0) > 0)
    return {"side": side, "stats": s, "z_crit_is": round(zcrit, 2), "k_tests": k_tests, "cut": str(cut)[:10], "phase": phase,
            "t_method": f"Newey-West lag={h['horizon_days']}，每日一个观测", "passed": bool(ok and phase == "confirm"),
            "explore_passed": bool(ok and phase != "confirm")}


def load_data(cfg) -> dict:
    from desk.tools import history
    return {c["inst_id"]: history(c["inst_id"], "1Dutc", cfg["research"]["history_bars"]) for c in cfg["coins"]}


def _hid(role, h):
    return f"H-{settings.today().replace('-', '')}-{role}-" + hashlib.sha1(json.dumps(h, sort_keys=True).encode()).hexdigest()[:6]


PROPOSE_NOTES: dict = {}


def propose(cfg, role, tracker, n) -> list[dict]:
    past = [r for r in settings.read_jsonl(REGISTRY) if r["role"] == role][-30:]
    res = {r["id"]: r for r in settings.read_jsonl(RESULTS)}
    hist = "\n".join(f"- {p['hypothesis']['name']}：{json.dumps(p['hypothesis']['conditions'], ensure_ascii=False)} "
                     f"{SIDE_CN[side_of(p['hypothesis'])]} h={p['hypothesis']['horizon_days']} → {'通过' if res.get(p['id'], {}).get('passed') else ('跳过(未知方向)' if res.get(p['id'], {}).get('skipped') else '未通过')}" for p in past) or "（无）"
    allowed = role_features(cfg, role)
    menu = "\n".join(f"- {k}：{FEATURES[k]}" for k in allowed)
    only = (f"你只能使用 {allowed[0]} 这一个特征（可用 1~3 个关于它的不同阈值条件），不得使用任何其他特征。\n" if len(allowed) == 1
            else f"你只能使用下列 {len(allowed)} 个特征，使用菜单外特征的假设会被整条拒绝。\n")
    prompt = (f"你是交易台的【{ROLE_CN[role]}】。本周请从你的专业视角提出最多 {n} 条（做多与做空合计）可在 OKX 日线历史上检验的假设，每条必须给出 side：long（做多）或 short（做空）。\n"
              "只能用下面你的角色专属特征菜单（全部在日线收盘时可得，无未来函数）；条件全部满足时下一根日线开盘按 side 入场（做空时止损在入场价上方），持有 horizon_days 天或触及止损。\n"
              "做多与同期无条件涨跌比较，做空与同期自然跌幅比较；多空假设一起计入多重检验数。\n"
              "检验会做样本内/样本外切分，探索期通过的只进候选，季度确认时按全局累计检验数做多重检验校正——所以请少而精，不要做参数网格搜索，不要重复以前试过的。\n"
              + only + f"\n特征菜单：\n{menu}\n\n你以前的假设：\n{hist}\n\n你当前手册：\n{roles.current_text(role)}")
    tok = dllm.STAGE.set(f"每周研究/{role}")
    try:
        llm = dllm.make_llm(cfg, "quick", tracker, role=role).with_structured_output(HypoList)
        hs = None
        # DeepSeek 等模型不接受 tool_choice，结构化输出只“绑定”工具而不强制调用；模型偶尔直接回文本，
        # 解析器就返回 None。重试一次并明确要求调用工具；仍为空则视为本角色无假设，不报错。
        for attempt in range(2):
            p = prompt if attempt == 0 else prompt + "\n\n【重要】请务必通过调用 HypoList 工具/结构化输出返回结果，不要只输出文字。"
            out = llm.invoke(p)
            if out is None:
                continue
            hs = out.hypotheses if hasattr(out, "hypotheses") else (HypoList(**out).hypotheses if isinstance(out, dict) else None)
            if hs is not None:
                break
        if hs is None:
            PROPOSE_NOTES[role] = "本角色无假设（结构化输出两次为空）"
            hs = []
    finally:
        dllm.STAGE.reset(tok)
    good = []
    for h in hs[:n]:
        d = h.model_dump() if hasattr(h, "model_dump") else dict(h)
        d["side"] = side_of(d)
        bad = [c["feature"] for c in d["conditions"] if c["feature"] not in allowed]
        if bad:      # S2：含菜单外特征 → 整条在检验前拒绝，不登记、不计入多重检验数
            settings.append_jsonl(REJECTED, {"time": settings.now_str(), "role": role, "hypothesis": d,
                                             "why": f"菜单外特征 {','.join(sorted(set(bad)))}"})
            continue
        d["conditions"] = d["conditions"][:3]
        d["stop_pct"] = min(max(float(d.get("stop_pct") or 8), 2), 15)
        if d["conditions"]:
            good.append(d)
    return good


def rule_text(h, r) -> str:
    cond = " 且 ".join(f"{FEATURES[c['feature']].split('（')[0]}({c['feature']}) {c['op']} {c['value']:g}" for c in h["conditions"])
    s = r["stats"]
    side = side_of(h)
    return (f"- 【研究验证 {r['id']}】当 {cond} 时，历史上随后 {h['horizon_days']} 天{SIDE_CN[side]}的超额收益为正"
            f"（样本内 {s['is']['n']} 笔 均值 {s['is']['mean_excess_pct']:+.2f}% t={s['is']['t']}；样本外 {s['oos']['n']} 笔 "
            f"均值 {s['oos']['mean_excess_pct']:+.2f}% t={s['oos']['t']}）。条件出现时可作为{'偏多' if side == 'long' else '偏空'}证据之一，但不单独决定方向。")


def run(cfg, tracker, force=False, only_roles=None) -> dict:
    R = cfg["research"]
    now = settings.now()
    week = now.strftime("%G-W%V")
    last = settings.read_json(LAST, {})
    if not force and (not R.get("enabled") or now.weekday() != R["weekday"] or last.get("week") == week):
        return {"skipped": True}
    RDIR.mkdir(parents=True, exist_ok=True)
    data = load_data(cfg)
    base_cost = tracker.summary() if tracker else {"cost_usd": 0, "input": 0, "output": 0}
    report = {"week": week, "time": settings.now_str(), "roles": {}, "edits": [], "stopped": None}
    for role in (only_roles or R["roles"]):
        u = tracker.summary() if tracker else base_cost
        if (u["cost_usd"] - base_cost["cost_usd"] > R["cost_cap_usd"]
                or (u["input"] + u["output"]) - (base_cost["input"] + base_cost["output"]) > R["token_cap"]):
            report["stopped"] = f"达到成本/token 上限，{role} 及之后角色本周不研究"
            break
        try:
            hs = propose(cfg, role, tracker, R["max_hypotheses_per_role"])
        except Exception as e:
            report["roles"][role] = {"error": f"{type(e).__name__}: {e}"[:200]}
            continue
        rows = []
        if not hs:
            report["roles"][role] = {"tested": [], "note": PROPOSE_NOTES.pop(role, "本角色无假设")}
            continue
        for h in hs:
            h = {**h, "side": side_of(h)}          # 登记前规范化：缺失/非法 → unknown
            hid = _hid(role, h)
            settings.append_jsonl(REGISTRY, {"id": hid, "role": role, "registered": settings.now_str(), "hypothesis": h,
                                             "test_plan": {"oos_frac": R["oos_frac"], "fee": R["fee"], "alpha": R["alpha"],
                                                           "min_trades_is": R["min_trades_is"], "min_trades_oos": R["min_trades_oos"],
                                                           "min_t_oos": R["min_t_oos"]}})
            if side_of(h) == "unknown":
                r = {"id": hid, "role": role, "tested": settings.now_str(), **_skipped(h)}
            else:
                k = global_k()
                r = {"id": hid, "role": role, "tested": settings.now_str(), **evaluate(h, data, cfg, k)}
            settings.append_jsonl(RESULTS, r)
            rows.append({"hypothesis": h, **r})
        report["roles"][role] = {"tested": rows}
        for x in rows:      # S2：探索期通过只进候选，等季度确认
            if x.get("explore_passed"):
                settings.append_jsonl(RDIR / "candidates.jsonl", {"time": settings.now_str(), "role": role, "id": x["id"],
                                                                  "rule": rule_text(x["hypothesis"], x), "week": week,
                                                                  "phase": "explore"})
        passed = [x for x in rows if x["passed"] and not x.get("skipped")][: cfg["learning"]["max_changes_per_edit"]]
        if passed and not cfg["learning"].get("research_write_playbook", False):   # 学习冻结（L1）：只写候选清单
            for x in passed:
                settings.append_jsonl(RDIR / "candidates.jsonl", {"time": settings.now_str(), "role": role, "id": x["id"],
                                                                  "rule": rule_text(x["hypothesis"], x), "week": week})
            report["edits"].append({"role": role, "type": "候选（学习冻结，未写手册）", "ids": [x["id"] for x in passed]})
            continue
        if passed:
            m = roles.read_meta(role)
            cur = next(x for x in m["history"] if x["version"] == m["current"])
            if cur.get("eval_status") == "pending":
                report["edits"].append({"role": role, "type": "推迟写入", "why": "手册仍在观察期", "ids": [x["id"] for x in passed]})
                continue
            learned = roles.learned_section(roles.current_text(role))
            if "暂无经验规则" in learned:
                learned = ""
            learned = (learned + "\n" + "\n".join(rule_text(x["hypothesis"], x) for x in passed)).strip()
            sc = scoring.scores(role)
            base = (sum(s["hit"] for s in sc[-20:]) / len(sc[-20:])) if len(sc) >= 20 else 0.5
            v = roles.save_new_version(role, learned, "每周研究：预注册假设通过样本内/样本外检验",
                                       [{"op": "新增", "rule": rule_text(x["hypothesis"], x), "why": x["id"]} for x in passed],
                                       base, min(len(sc), 20), len(sc))
            ev = {"role": role, "type": "修订", "version": v, "baseline_hit": base, "source": "research",
                  "changes": [{"op": "新增", "rule": rule_text(x["hypothesis"], x), "why": x["id"]} for x in passed],
                  "time": settings.now_str()}
            settings.append_jsonl(settings.STATE / "playbook_changes.jsonl", ev)
            report["edits"].append(ev)
    u = tracker.summary() if tracker else base_cost
    report["cost_usd"] = round(u["cost_usd"] - base_cost["cost_usd"], 4)
    report["tokens"] = (u["input"] + u["output"]) - (base_cost["input"] + base_cost["output"])
    settings.write_json(LAST, {"week": week, "time": settings.now_str()})
    settings.write_json(RDIR / f"report-{week}.json", report)
    write_md(report)
    return report


def write_md(rep):
    L = [f"# 每周自研报告 {rep['week']}", "", f"运行时间 {rep['time']}（北京时间）；LLM 成本约 ${rep.get('cost_usd', 0):.4f}，tokens {rep.get('tokens', 0):,}", ""]
    if rep.get("stopped"):
        L.append(f"⚠️ {rep['stopped']}\n")
    for role, x in rep["roles"].items():
        L += [f"## {ROLE_CN[role]}", ""]
        if "error" in x:
            L.append(f"失败：{x['error']}"); continue
        if not x.get("tested"):
            L.append(x.get("note", "本角色无假设") + "\n"); continue
        L += ["| 编号 | 方向 | 假设 | 条件 | 持有 | 样本内 n/均值/t | 门槛t | 样本外 n/均值/t | 结论 |", "|---|---|---|---|---|---|---|---|---|"]
        for r in [r for r in x["tested"] if not r.get("skipped")]:
            h, s = r["hypothesis"], r["stats"]
            cond = "；".join(f"{c['feature']}{c['op']}{c['value']:g}" for c in h["conditions"])
            L.append(f"| {r['id']} | {SIDE_CN[side_of(h)]} | {h['name']} | {cond} | {h['horizon_days']}天 | {s['is']['n']}/{s['is']['mean_excess_pct']}/{s['is']['t']} | "
                     f"{r['z_crit_is']} | {s['oos']['n']}/{s['oos']['mean_excess_pct']}/{s['oos']['t']} | {'✅通过' if r['passed'] else '❌未通过'} |")
        sk = [r for r in x["tested"] if r.get("skipped")]
        if sk:
            L += ["", "**未知方向，跳过检验**（side 缺失或不是 long/short；不检验、不写手册、不计入 Bonferroni 检验数）", "",
                  "| 编号 | 方向 | 假设 | 条件 | 持有 |", "|---|---|---|---|---|"]
            for r in sk:
                h = r["hypothesis"]
                cond = "；".join(f"{c['feature']}{c['op']}{c['value']:g}" for c in h["conditions"])
                L.append(f"| {r['id']} | 未知 | {h['name']} | {cond} | {h['horizon_days']}天 |")
        L.append("")
    L += ["## 手册写入", ""] + ([f"- {ROLE_CN[e['role']]}：{e['type']} " + (f"v{e['version']:03d}" if e.get('version') else e.get('why', '')) for e in rep["edits"]] or ["无"])
    (RDIR / f"report-{rep['week']}.md").write_text("\n".join(L) + "\n", encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--roles", default="")
    ap.add_argument("--mock", action="store_true")
    a = ap.parse_args(argv)
    import os
    if a.mock:
        os.environ["TA_DESK_MOCK_LLM"] = "1"
    cfg = settings.load_config()
    dllm.prepare_env(cfg)
    roles.ensure_playbooks()
    tr = dllm.UsageTracker(cfg)
    rep = run(cfg, tr, force=a.force, only_roles=[r for r in a.roles.split(",") if r] or None)
    print(json.dumps({k: v for k, v in rep.items() if k != "roles"}, ensure_ascii=False, default=str)[:2000])
    print(json.dumps(tr.summary()["by_model"], ensure_ascii=False))


if __name__ == "__main__":
    main()


def confirm_quarter(cfg, data: dict, quarter: str) -> list[dict]:
    """S2：季度确认。把探索期通过的候选按确认期规则重检，门槛用全局累计检验数做 Bonferroni。
    结果写入 results.jsonl（confirm_of 指向原假设）；不写手册，学习冻结期间只作记录。"""
    reg = {x["id"]: x for x in settings.read_jsonl(REGISTRY)}
    seen = {x.get("confirm_of") for x in settings.read_jsonl(RESULTS) if x.get("confirm_of")}
    ccfg = {**cfg, "research": {**cfg["research"], "phase": "confirm"}}
    k = global_k(0)
    out = []
    for c in settings.read_jsonl(RDIR / "candidates.jsonl"):
        if c.get("phase") != "explore" or c["id"] in seen or c["id"] not in reg:
            continue
        h = {**reg[c["id"]]["hypothesis"], "side": side_of(reg[c["id"]]["hypothesis"])}
        r = {"id": c["id"] + "-C" + quarter, "confirm_of": c["id"], "role": reg[c["id"]]["role"], "quarter": quarter,
             "tested": settings.now_str(), **evaluate(h, data, ccfg, k)}
        settings.append_jsonl(RESULTS, r); out.append(r); seen.add(c["id"])
    return out
