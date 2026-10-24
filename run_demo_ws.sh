#!/usr/bin/env bash
# OKX 模拟盘 WebSocket 常驻监听（只监听不下单，禁止实盘）。
#   setsid nohup bash run_demo_ws.sh >/dev/null 2>&1 < /dev/null &
set -uo pipefail
cd "$(dirname "$0")"
[ -f state/secrets.env ] && set -a && . state/secrets.env && set +a
for _v in $(compgen -e); do case "$_v" in OKX_DEMO_*) ;; OKX_*KEY*|OKX_*SECRET*|OKX_*PASS*|OKX_*TOKEN*) unset "$_v" ;; esac; done; unset _v   # 2b：清除实盘 OKX 凭据变量（只按变量名，不读值），只保留 OKX_DEMO_*
mkdir -p logs
. "$PWD/scripts/notify_fallback.sh"; export TA_ALERT_SECRETS="$PWD/state/secrets.env"
if [ ! -x .venv/bin/python ]; then ta_alert demo_ws_skipped "模拟盘 WS 监听 run_demo_ws.sh 未启动：Python 解释器缺失（.venv/bin/python），请检查 .venv" "logs/demo-ws-$(TZ=Asia/Shanghai date +%F).log"; exit 5; fi
while true; do
  .venv/bin/python -m desk.demo_ws >> "logs/demo-ws-$(TZ=Asia/Shanghai date +%F).log" 2>&1
  rc=$?
  [ "$rc" = "127" ] || [ ! -x .venv/bin/python ] && { ta_alert demo_ws_skipped "模拟盘 WS 监听已退出：Python 解释器不可用（返回码 $rc），请检查 .venv" "logs/demo-ws-$(TZ=Asia/Shanghai date +%F).log"; exit 5; }
  [ "$rc" = "1" ] && pgrep -f "python -m desk.demo_ws" >/dev/null && exit 0   # 已有实例
  sleep 10
done
