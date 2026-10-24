"""只用新代码重新渲染某日日报，不重跑分析、不下单、不改任何订单。
数据来源：state/decisions.jsonl（该日每币最后一次决策，含重跑）、state/usage/run-*.json（多次运行合并）、模拟盘只读账户快照。
用法：python -m scripts.rerender_report 2026-10-21 --usage run-A.json[:标签] ... [--error 文本] [--note INST=文本] [--name 文件名]"""
import argparse
import sys

from desk import settings


def _unpriced_from_log(u, cfg):
    """旧的 run-*.json 没有 unpriced_calls：按 run_id 从 state/usage/<日期>.jsonl 逐条补算（单价未知的调用数）。"""
    if cfg is None or "unpriced_calls" in u or not u.get("run_id"):
        return u
    from desk import llm as dllm
    rid = u["run_id"]; day = f"{rid[:4]}-{rid[4:6]}-{rid[6:8]}"
    recs = [r for r in settings.read_jsonl(settings.STATE / "usage" / f"{day}.jsonl") if r.get("run_id") == rid and "error" not in r]
    u = {**u, "unpriced_calls": 0}
    for sec in ("by_stage", "by_model", "by_provider"):
        u[sec] = {k: {**v, "unpriced_calls": 0} for k, v in u.get(sec, {}).items()}
    for r in recs:
        if dllm.price_known(cfg, r["model"]):
            continue
        u["unpriced_calls"] += 1
        for sec, k in (("by_stage", r["stage"]), ("by_model", r["model"]), ("by_provider", r.get("provider", "?"))):
            if k in u[sec]:
                u[sec][k]["unpriced_calls"] += 1
    return u


def merge_usage(items, cfg=None):
    tot = {"calls": 0, "errors": 0, "input": 0, "cached": 0, "output": 0, "cost_usd": 0.0, "unpriced_calls": 0,
           "by_stage": {}, "by_model": {}, "by_provider": {}}
    for u, tag in items:
        u = _unpriced_from_log(u, cfg)
        for k in ("calls", "errors", "input", "cached", "output", "cost_usd", "unpriced_calls"):
            tot[k] += u.get(k, 0)
        for sec in ("by_stage", "by_model", "by_provider"):
            for name, v in u.get(sec, {}).items():
                key = f"{name}（{tag}）" if (sec == "by_stage" and tag) else name
                d = tot[sec].setdefault(key, {"calls": 0, "input": 0, "cached": 0, "output": 0, "cost_usd": 0.0, "unpriced_calls": 0})
                for k in d:
                    d[k] += v.get(k, 0)
    return tot


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("date"); ap.add_argument("--usage", action="append", default=[])
    ap.add_argument("--error", action="append", default=[]); ap.add_argument("--note", action="append", default=[])
    ap.add_argument("--name", default="")
    a = ap.parse_args(argv)
    from desk import ledger, okx_data, report
    cfg = settings.load_config(); ledger.assert_paper(cfg)
    last = {}
    for d in settings.read_jsonl(settings.STATE / "decisions.jsonl"):
        if d["date"] == a.date:
            last[d["inst_id"]] = d
    order = [c["inst_id"] for c in cfg["coins"]]
    decisions = sorted(last.values(), key=lambda d: order.index(d["inst_id"]) if d["inst_id"] in order else 99)
    items = []
    for s in a.usage:
        f, _, tag = s.partition(":")
        items.append((settings.read_json(settings.STATE / "usage" / f, {}), tag))
    notes = {}
    for n in a.note:
        k, _, v = n.partition("=")
        notes.setdefault(k, []).append(v)
    # 同日补做的平仓复盘（state/reviews/*.json 里复盘时间在该日的）：带进“今日平仓与复盘会”段，不重新调用 LLM
    from desk import review
    revs = sorted((settings.read_json(p, {}) for p in review.REVIEWS.glob("*.json")) if review.REVIEWS.exists() else [],
                  key=lambda r: r.get("time", ""))
    revs = [r for r in revs if str(r.get("time", "")).startswith(a.date)]
    closed = [r["trade"] for r in revs]
    prices = {c["inst_id"]: okx_data.last_price(c["inst_id"]) for c in cfg["coins"]}   # 同一次快照，全报告共用
    p = report.write(cfg, a.date, decisions, closed, revs, [], [], merge_usage(items, cfg), prices, a.error,
                     name=a.name or None, coin_notes=notes, prices_time=settings.now_str())
    print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
