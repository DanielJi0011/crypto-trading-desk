#!/usr/bin/env python3
"""模型漂移金丝雀（2026-11-22 审查共识包 G 项）：每周把存档判定特征表经生产判定路径重放，
探知中转/模型侧的静默漂移（换模型、改参数、行为漂移）。

做法：从 state/ledger/regime_calls.jsonl 均匀取 6 份跨时段存档（特征表 + 原判），
用对应决策记录的 market 快照重建 mk，按生产同路（regime_judge.judge_prompt →
call_judge → build_regime 生效 stage）重放，记录方向/强度/阶段/confidence、响应
model 字段（取自 UsageTracker 的逐调用记录）、延迟、token 数，落
state/iteration_reports/canary-<日期>.json。

已知近似（写明，不假装精确）：原场次的共识 S/票数与市场分析师摘录未随判定存档，
重放提示词这两处用中性值（S=0、无摘录）——所以与原判的个别翻转可能含语境差异成分；
金丝雀的主信号是「与上周金丝雀同口径结果」的翻转数与 model 字段变化，原判对照为辅。
告警线：方向或阶段翻转 ≥2/6（对上周金丝雀口径），或 model 字段集合变化 → 输出
CANARY_ALERT 行（周 cron 据此向主聊天告警），否则只打印 OK 摘要、静默落盘。

只读于交易状态：不下单、不写决策/账本；UsageTracker 的逐调用记录改道写
iteration_reports/usage-canary-<日期>.jsonl，不污染生产用量账。
用法：cd /opt/ta-desk && set -a && . state/secrets.env && set +a && .venv/bin/python scripts/canary_replay.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from desk import llm as dllm
from desk import manager
from desk import regime_judge as RJ
from desk import settings

N_SAMPLES = 6


def pick_samples(rows: list[dict], n: int = N_SAMPLES) -> list[dict]:
    ok = [r for r in rows if not r.get("failed") and r.get("judge") and r.get("features")]
    if len(ok) <= n:
        return ok
    idx = sorted({round(i * (len(ok) - 1) / (n - 1)) for i in range(n)})
    return [ok[i] for i in idx]


def main() -> int:
    cfg = settings.load_config()
    rows = settings.read_jsonl(RJ.REGIME_CALLS)
    samples = pick_samples(rows)
    decs = {r.get("decision_id"): r for r in settings.read_jsonl(manager.DECISIONS)}
    names = {c["inst_id"]: c["name"] for c in cfg["coins"]}
    out_dir = settings.STATE / "iteration_reports"
    out_dir.mkdir(parents=True, exist_ok=True)

    tracker = dllm.UsageTracker(cfg)
    tracker.path = out_dir / f"usage-canary-{settings.today()}.jsonl"   # 不污染生产用量账

    results = []
    for r in samples:
        dec = decs.get(r.get("decision_id")) or {}
        mk = dec.get("market") or {}
        feat = r["features"]
        prompt = RJ.judge_prompt(names.get(r["inst_id"], r["inst_id"]), r["inst_id"], feat, mk,
                                 0.0, {}, "")
        before = len(tracker.calls)
        t0 = time.monotonic()
        judge = RJ.call_judge(cfg, tracker, prompt)
        latency = round(time.monotonic() - t0, 2)
        new_calls = [c for c in tracker.calls[before:] if "error" not in c]
        usage = new_calls[-1] if new_calls else {}
        regime = RJ.build_regime(cfg, judge, feat) if judge else None
        eff = (regime or {}).get("judge") or {}
        orig = r["judge"] or {}
        results.append({
            "decision_id": r.get("decision_id"), "date": r.get("date"), "inst": r["inst_id"],
            "orig": {"direction": orig.get("direction"), "strength": orig.get("strength"),
                     "stage": orig.get("stage"), "stage_raw": orig.get("stage_raw"),
                     "confidence": orig.get("confidence")},
            "replay": ({"direction": eff.get("direction"), "strength": eff.get("strength"),
                        "stage": eff.get("stage"), "stage_raw": eff.get("stage_raw"),
                        "confidence": eff.get("confidence")} if judge else None),
            "model": usage.get("model"), "key": usage.get("key"),
            "latency_s": latency, "input": usage.get("input"), "output": usage.get("output"),
            "flip_vs_orig": bool(judge) and (eff.get("direction") != orig.get("direction")
                                             or eff.get("stage") != orig.get("stage")),
        })

    models = sorted({x["model"] for x in results if x["model"]})
    report = {"date": settings.today(), "n": len(results), "models": models, "samples": results}

    # 与上周金丝雀同口径比对（同一批 decision_id 的重放结果）
    prev_files = sorted(out_dir.glob("canary-*.json"))
    prev = None
    for p in reversed(prev_files):
        try:
            cand = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if cand.get("date") != report["date"]:
            prev = cand
            break
    flips_vs_prev, model_changed = None, None
    if prev:
        prev_map = {s["decision_id"]: s for s in prev.get("samples") or []}
        flips_vs_prev = 0
        for s in results:
            p0 = prev_map.get(s["decision_id"])
            if p0 and p0.get("replay") and s.get("replay"):
                if (p0["replay"]["direction"] != s["replay"]["direction"]
                        or p0["replay"]["stage"] != s["replay"]["stage"]):
                    flips_vs_prev += 1
        model_changed = prev.get("models") != models
        report["flips_vs_prev_canary"] = flips_vs_prev
        report["model_changed_vs_prev"] = model_changed

    out_path = out_dir / f"canary-{settings.today()}.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    flips_orig = sum(1 for x in results if x["flip_vs_orig"])
    print(f"canary: {len(results)} samples, models={models}, flips_vs_orig={flips_orig}, "
          f"flips_vs_prev={flips_vs_prev}, model_changed={model_changed} -> {out_path.name}")
    if (flips_vs_prev is not None and flips_vs_prev >= 2) or model_changed:
        print(f"CANARY_ALERT: flips_vs_prev={flips_vs_prev}, model_changed={model_changed}, models={models}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
