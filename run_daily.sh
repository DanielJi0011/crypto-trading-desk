#!/usr/bin/env bash
[ -z "${TA_DESK_HOME:-}" ] && [ -f /workspace/ta-desk/state/secrets.env ] && set -a && . /workspace/ta-desk/state/secrets.env && set +a
for _v in $(compgen -e); do case "$_v" in OKX_DEMO_*) ;; OKX_*KEY*|OKX_*SECRET*|OKX_*PASS*|OKX_*TOKEN*) unset "$_v" ;; esac; done; unset _v   # 2b：清除实盘 OKX 凭据变量（只按变量名，不读值），只保留 OKX_DEMO_*
# 设置了 TA_DESK_HOME（测试/临时数据目录）时不加载真实 secrets.env。
# 每日入口：bash run_daily.sh [--session morning|evening] [--coins BTC,ETH,SOL] [--mock] [--date YYYY-MM-DD]
# 两场共用单实例锁；晚场标记 state/daily_done/<日期>-evening、日志 logs/run-<日期>-evening.log、报告 reports/<日期>-evening.md。
# 北京时间 08:00 运行（OKX 1Dutc 日线刚收盘，DeepSeek 此时为半价时段）。
# 整改第一批 2c：
#   单实例锁 state/locks/run_daily.lock（flock -n，拿不到锁 = 正在运行 → 记 daily_duplicate_trigger 后退出 0）；
#   完成标记 state/daily_done/<日期>（已存在 → 记 daily_duplicate_trigger 后退出 0，不再进入交易流程）；
#   失败写 state/daily_failed/<日期>（内容为返回码）并发 webhook daily_failed。--mock 演练不读写完成/失败标记。
#   O1 时间窗口：非 --mock 时只在 config sessions 窗口内运行（早场北京 07:55 到 12:00，晚场纽约 08:25 到 10:30），窗口外记 daily_duplicate_trigger(out_of_window) 后退出 0；
#   需要窗口外补跑时显式设置 TA_DAILY_IGNORE_WINDOW=1。成功后记事件并发 daily_done 通知。
set -uo pipefail
cd "$(dirname "$0")"
DATA="${TA_DESK_HOME:-$PWD}"                 # 数据目录（测试时指向临时目录）
PY="${TA_DESK_PYTHON:-$PWD/.venv/bin/python}"   # 默认用仓库 .venv；测试/无 .venv 环境可用 TA_DESK_PYTHON 覆盖
RUNNER="${TA_DAILY_RUNNER:-$PY -m desk.run_daily}"   # 仅测试用：替换为桩命令，不跑真正的交易流程
mkdir -p "$DATA/logs" "$DATA/state/locks" "$DATA/state/daily_done" "$DATA/state/daily_failed"
DAY="$(TZ=Asia/Shanghai date +%F)"
MOCK=0; prev=""; SESSION=morning
for a in "$@"; do
  [ "$a" = "--mock" ] && MOCK=1
  [ "$prev" = "--date" ] && DAY="$a"
  [ "$prev" = "--session" ] && SESSION="$a"
  case "$a" in --session=*) SESSION="${a#--session=}" ;; esac
  prev="$a"
done
case "$SESSION" in morning) SFX="" ;; evening) SFX="-evening" ;; *) echo "[run_daily.sh] 未知场次 $SESSION（只支持 morning/evening）"; exit 2 ;; esac
KEY="$DAY$SFX"                                  # 标记名：早场 <日期>，晚场 <日期>-evening
LOG="$DATA/logs/run-$(TZ=Asia/Shanghai date +%F)$SFX.log"
exec 9>"$DATA/state/locks/run_daily.lock"
if ! flock -n 9; then
  echo "[run_daily.sh] 已有实例在运行，本次退出（daily_duplicate_trigger running）" | tee -a "$LOG"
  "$PY" -m desk.daily_gate duplicate "$KEY" running >>"$LOG" 2>&1
  exit 0
fi
NOW_HM="${TA_DAILY_NOW:-$(TZ=Asia/Shanghai date +%H:%M)}"   # TA_DAILY_NOW 仅测试用（北京 HH:MM）
# 窗口按 config sessions.<场次> 用 zoneinfo 换算成北京时间（晚场按纽约时间，随夏令时切换）
if [ "$MOCK" = "0" ] && [ "${TA_DAILY_IGNORE_WINDOW:-0}" != "1" ]; then
  WIN=$(TA_DESK_HOME="$DATA" "$PY" -m desk.sessions window "$SESSION" "$DAY" "$NOW_HM"); wrc=$?
  if [ "$wrc" != "0" ]; then
    echo "[run_daily.sh] 当前北京时间 $NOW_HM 不在 $SESSION 窗口 $WIN（北京时间）内，本次退出（daily_duplicate_trigger out_of_window）" | tee -a "$LOG"
    "$PY" -m desk.daily_gate duplicate "$KEY" out_of_window >>"$LOG" 2>&1
    exit 0
  fi
fi
if [ "$MOCK" = "0" ] && [ -f "$DATA/state/daily_done/$KEY" ]; then
  echo "[run_daily.sh] $KEY 已完成（$DATA/state/daily_done/$KEY），本次退出（daily_duplicate_trigger done）" | tee -a "$LOG"
  "$PY" -m desk.daily_gate duplicate "$KEY" done >>"$LOG" 2>&1
  exit 0
fi
$RUNNER "$@" 2>&1 | tee -a "$LOG"
rc=${PIPESTATUS[0]}
if [ "$MOCK" = "0" ]; then
  if [ "$rc" = "0" ]; then
    TZ=Asia/Shanghai date "+%F %T%z" > "$DATA/state/daily_done/$KEY"
    rm -f "$DATA/state/daily_failed/$KEY"
    "$PY" -m desk.daily_gate done "$KEY" 0 >>"$LOG" 2>&1
    # 成功后自动备份 state（不含密钥文件与锁）；备份失败只记日志，不改变返回码
    TA_DESK_HOME="$DATA" timeout 300 bash "$PWD/scripts/backup_state.sh" >>"$LOG" 2>&1 || echo "[run_daily.sh] state 备份失败（不影响日流程结果）" | tee -a "$LOG"
B=$(ls -1t "$DATA"/backups/state-*.tar.gz 2>/dev/null | head -1)   # 离机上传（sftp）：失败只告警 offsite_backup_failed，不影响日报
[ -n "$B" ] && { TA_DESK_HOME="$DATA" timeout 700 "$PY" -m desk.offsite upload "$B" >>"$LOG" 2>&1 || echo "[run_daily.sh] 离机备份上传失败（已告警，不影响日流程结果）" | tee -a "$LOG"; }
  else
    echo "$rc" > "$DATA/state/daily_failed/$KEY"
    "$PY" -m desk.daily_gate failed "$KEY" "$rc" >>"$LOG" 2>&1
  fi
fi
exit "$rc"
