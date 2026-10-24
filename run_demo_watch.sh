#!/usr/bin/env bash
[ -f /workspace/ta-desk/state/secrets.env ] && set -a && . /workspace/ta-desk/state/secrets.env && set +a
for _v in $(compgen -e); do case "$_v" in OKX_DEMO_*) ;; OKX_*KEY*|OKX_*SECRET*|OKX_*PASS*|OKX_*TOKEN*) unset "$_v" ;; esac; done; unset _v   # 2b：清除实盘 OKX 凭据变量（只按变量名，不读值），只保留 OKX_DEMO_*
# OKX 模拟盘轻量轮询（禁止实盘）：条件单到期/成交、持有期到期平仓、止损检测→再讨论。
#   bash run_demo_watch.sh             → 运行一次
#   bash run_demo_watch.sh --loop 300  → 常驻循环
set -uo pipefail
cd "$(dirname "$0")"
mkdir -p logs
. "$PWD/scripts/notify_fallback.sh"; export TA_ALERT_SECRETS="$PWD/state/secrets.env"
if [ ! -x .venv/bin/python ]; then ta_alert demo_watch_skipped "模拟盘轮询 run_demo_watch.sh 未启动：Python 解释器缺失（.venv/bin/python），请检查 .venv" "logs/demo-watch-$(TZ=Asia/Shanghai date +%F).log"; exit 5; fi
once() { timeout -k 10 150 .venv/bin/python -m desk.demo_watch --once >> "logs/demo-watch-$(TZ=Asia/Shanghai date +%F).log" 2>&1; }
if [ "${1:-}" = "--loop" ]; then
  while true; do once; sleep "${2:-300}"; done
else
  once
fi
