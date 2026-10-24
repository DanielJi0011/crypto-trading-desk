#!/usr/bin/env bash
[ -f /workspace/ta-desk/state/secrets.env ] && set -a && . /workspace/ta-desk/state/secrets.env && set +a
for _v in $(compgen -e); do case "$_v" in OKX_DEMO_*) ;; OKX_*KEY*|OKX_*SECRET*|OKX_*PASS*|OKX_*TOKEN*) unset "$_v" ;; esac; done; unset _v   # 2b：清除实盘 OKX 凭据变量（只按变量名，不读值），只保留 OKX_DEMO_*
# 【已停用 2026-10-20：用户要求彻底停掉，不要再启动】旧的本地纸面盘中监控（仅 execution.venue=paper 时有意义）。
# 模拟盘直连流程请用 run_demo_watch.sh。
set -uo pipefail
cd "$(dirname "$0")"
mkdir -p logs
. "$PWD/scripts/notify_fallback.sh"; export TA_ALERT_SECRETS="$PWD/state/secrets.env"
if [ ! -x .venv/bin/python ]; then ta_alert monitor_skipped "旧监控 run_monitor.sh 未启动：Python 解释器缺失（.venv/bin/python），请检查 .venv" "logs/monitor-$(TZ=Asia/Shanghai date +%F).log"; exit 5; fi
once() { .venv/bin/python -m desk.monitor --once >> "logs/monitor-$(TZ=Asia/Shanghai date +%F).log" 2>&1; }
if [ "${1:-}" = "--loop" ]; then
  while true; do once; sleep "${2:-300}"; done
else
  once
fi
