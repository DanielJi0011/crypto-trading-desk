#!/usr/bin/env bash
# 本机无 cron/crontab：用常驻循环替代，每 10 分钟跑一次纯脚本健康检查（不调用 LLM）。
#   setsid nohup bash scripts/healthcheck_loop.sh >/dev/null 2>&1 < /dev/null &
cd "$(dirname "$0")/.." || exit 1
for _v in $(compgen -e); do case "$_v" in OKX_DEMO_*) ;; OKX_*KEY*|OKX_*SECRET*|OKX_*PASS*|OKX_*TOKEN*) unset "$_v" ;; esac; done; unset _v   # 2b：清除实盘 OKX 凭据变量（只按变量名，不读值），只保留 OKX_DEMO_*
while true; do
  timeout 120 .venv/bin/python scripts/healthcheck.py >> logs/healthcheck.log 2>&1
  sleep 600
done
