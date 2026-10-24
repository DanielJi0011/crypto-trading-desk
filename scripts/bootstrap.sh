#!/usr/bin/env bash
# O1 新机器恢复步骤（只写成脚本供人工逐步执行；本轮未运行）。不会创建或读取 secrets.env 内容，密钥由用户手工放入。
# 用法：bash scripts/bootstrap.sh [备份包路径]
set -euo pipefail
cd "$(dirname "$0")/.."
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -e vendor/TradingAgents pyyaml requests pandas pytest   # 本仓库无 requirements.txt，依赖清单待补（风险登记）
if [ -n "${1:-}" ]; then
  tar -xzf "$1" -C .            # 恢复 state/（备份不含 secrets.env）
fi
[ -f state/secrets.env ] || echo "提示：state/secrets.env 不存在，请手工放入 OKX_DEMO_* 与 LLM 变量（不要放实盘 key）"
.venv/bin/python -m pytest -q
echo "下一步（人工）：确认 KILL/HALT 状态，再按 README 启动 run_demo_ws.sh、run_demo_watch.sh --loop、scripts/healthcheck_loop.sh"
