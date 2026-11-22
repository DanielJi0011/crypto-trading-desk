#!/usr/bin/env python3
"""备用 key 故障演练（2026-11-22 审查共识包 H 项，一次性）：在非决策路径上验证主→备
切换全链路（2026-11-15 上线后从未在实盘触发过）。

方法（绝不改生产 secrets/config、绝不打印任何 key 值）：
- 本进程内构造一个 base_url 指向死地址（127.0.0.1:9，连接拒绝 = 可重试错误）的
  「主客户端」——请求根本发不出去，不消耗主 key 配额；
- 用生产函数 providers._register_backup 为它登记真实的备用孪生（真实中转 + 备用 key）；
- 经生产 llm_retry.install 的 invoke 包装发一句无害提示词，主 key 重试耗尽后应自动
  切换备用 key 接管；退避序列在本进程的 config 副本里缩短，不影响生产配置。
判读：SWITCHES 新增 1 条、最终响应成功、用量记录的 key 标签为 backup、总耗时。
用法：cd /opt/ta-desk && set -a && . state/secrets.env && set +a && .venv/bin/python scripts/failover_drill.py
"""
from __future__ import annotations

import copy
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from desk import llm as dllm
from desk import llm_retry
from desk import providers
from desk import settings

ROLE = "trade_manager"          # claude 渠道席位（判定/交易经理同席）
DEAD_URL = "http://127.0.0.1:9/v1"


def main() -> int:
    cfg = copy.deepcopy(settings.load_config())
    cfg["llm"]["max_retries"] = 0                       # 进程内副本：关掉客户端内层重试，演练快
    cfg["llm"]["retry_delays"] = [0.5, 1.0]             # 主 key 退避（进程内缩短）
    cfg["llm"]["backup_retry_delays"] = [0.5]           # 备用退避（进程内缩短）
    r = providers.resolve(cfg, ROLE)
    if r["provider"] != "claude":
        print(f"DRILL_ABORT: role {ROLE} provider={r['provider']} 非 claude 渠道，无主备切换")
        return 2
    if not os.environ.get(r["key_env"] + "_BACKUP"):
        print("DRILL_ABORT: 备用 key 环境变量缺失（本机未配置备用 key）")
        return 2
    if not os.environ.get(r["key_env"]):
        print("DRILL_ABORT: 主 key 环境变量缺失")
        return 2

    out_dir = settings.STATE / "iteration_reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    tracker = dllm.UsageTracker(cfg)
    tracker.path = out_dir / f"usage-drill-{settings.today()}.jsonl"   # 不污染生产用量账

    primary = providers._make_relay_client(cfg, {**r, "base_url": DEAD_URL}, tracker,
                                           os.environ[r["key_env"]])
    providers._register_backup(cfg, r, tracker, primary)
    if llm_retry.backup_twin(primary) is None:
        print("DRILL_ABORT: 备用孪生登记失败")
        return 2
    llm_retry.install(cfg)

    sw0, rt0 = len(llm_retry.SWITCHES), len(llm_retry.RETRIES)
    t0 = time.monotonic()
    resp = primary.invoke("Reply with the single word: ok")
    elapsed = round(time.monotonic() - t0, 2)
    text = (getattr(resp, "content", "") or "").strip()
    calls = [c for c in tracker.calls if "error" not in c]
    serving = calls[-1] if calls else {}
    result = {"role": ROLE, "model": r["model"], "primary_attempts": len(llm_retry.RETRIES) - rt0 + 1,
              "switches": len(llm_retry.SWITCHES) - sw0, "total_s": elapsed,
              "serving_key": serving.get("key"), "serving_model": serving.get("model"),
              "response_ok": bool(text), "response_head": text[:40]}
    print("DRILL_RESULT " + json.dumps(result, ensure_ascii=False))
    ok = result["switches"] == 1 and result["serving_key"] == "backup" and result["response_ok"]
    print("DRILL_PASS" if ok else "DRILL_FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
