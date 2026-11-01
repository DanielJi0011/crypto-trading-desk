#!/usr/bin/env bash
# 安装触发器（需用户同意后人工执行；默认只打印将做的改动）。
#   bash scripts/install_autostart.sh          → 只打印
#   bash scripts/install_autostart.sh --apply  → 写入 ~/.bashrc 钩子 + 启动后台轮询
# 盒子 PID1 为 tini/pod-daemon，无 systemd(offline)/cron/rc.local，没有"开机即执行"的用户钩子；
# 这里能做的最好方案：(a) 每个交互式终端打开时触发一次 ensure；(b) 常驻 5 分钟轮询兜底（重建后会消失，需再由 (a) 或人工拉起）。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="[ -x /usr/bin/flock ] && ( bash $ROOT/scripts/ensure_running.sh >/dev/null 2>&1 & )  # ta-desk-autostart"
POLL="while true; do bash $ROOT/scripts/ensure_running.sh; sleep 300; done"
if [ "${1:-}" != "--apply" ]; then
  echo "将追加到 ~/.bashrc: $HOOK"
  echo "将启动轮询: setsid nohup bash -c '$POLL' >/dev/null 2>&1 </dev/null &"
  exit 0
fi
grep -q 'ta-desk-autostart' ~/.bashrc || echo "$HOOK" >> ~/.bashrc
pgrep -f 'ensure_running.sh; sleep 300' >/dev/null || setsid nohup bash -c "$POLL" >/dev/null 2>&1 </dev/null &
echo installed
