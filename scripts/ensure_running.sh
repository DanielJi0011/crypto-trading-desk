#!/usr/bin/env bash
# 幂等守护：保证 .venv 存在、healthcheck_loop / demo_ws / demo_watch 三个常驻进程在跑。
# 纯 shell，不调用任何模型；不读取、不打印 state/secrets.env（由各进程自己加载）。只做模拟盘，不含任何下单逻辑。
# 用法：bash scripts/ensure_running.sh            正常执行
#       TA_ENSURE_DRY_RUN=1 bash scripts/ensure_running.sh  只打印将执行的动作
# 测试钩子（仅测试用）：TA_ENSURE_ROOT 指定根目录；TA_ENSURE_PGREP 替换 pgrep；TA_ENSURE_LOCK 指定锁文件。
set -uo pipefail
ROOT="${TA_ENSURE_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$ROOT" || exit 1
PGREP="${TA_ENSURE_PGREP:-pgrep}"
DRY="${TA_ENSURE_DRY_RUN:-0}"
mkdir -p logs
LOCK="${TA_ENSURE_LOCK:-$ROOT/logs/.ensure_running.lock}"
LOG="$ROOT/logs/ensure_running.log"

log() { echo "$(TZ=Asia/Shanghai date '+%F %T%z') $*" >> "$LOG"; [ "$DRY" = "1" ] && echo "$*"; return 0; }

exec 9>"$LOCK"
if ! flock -n 9; then log "SKIP locked"; exit 0; fi

# 1) venv
ensure_venv() {
  [ -x .venv/bin/python ] && return 0
  log "VENV missing -> rebuild"
  if [ "$DRY" = "1" ]; then echo "DRY venv rebuild"; return 0; fi
  local req; req="$(mktemp)"
  grep -v '^-e ' requirements.lock > "$req"      # TradingAgents 用 vendor 副本安装
  { python3 -m venv .venv \
    && .venv/bin/pip install -q -r "$req" \
    && .venv/bin/pip install -q --no-deps -e vendor/TradingAgents; } >> "$LOG" 2>&1
  local rc=$?; rm -f "$req"
  log "VENV rebuild rc=$rc"; return $rc
}

# 2) 进程：名称|pgrep 模式|启动命令（与原命令行一致）
start() {  # $@ = 命令
  log "START $*"
  [ "$DRY" = "1" ] && { echo "DRY env -u TA_NOTIFY_DISABLED -u TA_EVENT_SOURCE setsid nohup $*"; return 0; }
  env -u TA_NOTIFY_DISABLED -u TA_EVENT_SOURCE setsid nohup "$@" >/dev/null 2>&1 < /dev/null &
}
ensure_proc() {  # $1 模式, 其余=命令
  local pat="$1"; shift
  if "$PGREP" -f "$pat" >/dev/null 2>&1; then return 0; fi
  start "$@"
}

ensure_venv || log "WARN venv rebuild failed; processes will alert on their own"
ensure_proc 'bash scripts/healthcheck_loop\.sh' bash scripts/healthcheck_loop.sh   # 优先
ensure_proc 'bash run_demo_ws\.sh'               bash run_demo_ws.sh
ensure_proc 'bash run_demo_watch\.sh --loop'     bash run_demo_watch.sh --loop 300
exit 0
