#!/usr/bin/env bash
# O1 状态备份：把 state/ 打包到 backups/state-<北京时间>.tar.gz，排除 secrets.env、state/keys（私钥与主机指纹）与锁文件；只保留最近 14 份。
# 离机上传由 run_daily.sh 在本脚本之后调用 desk/offsite.py（sftp 到 <BACKUP_HOST>），本脚本只做本机备份。
# 本脚本不读取 secrets.env 内容，也不打印任何变量值。run_daily.sh 成功完成后自动调用一次（2026-10-23 用户批准）。
set -euo pipefail
cd "$(dirname "$0")/.."
DATA="${TA_DESK_HOME:-$PWD}"
OUT="$DATA/backups"; mkdir -p "$OUT"
F="$OUT/state-$(TZ=Asia/Shanghai date +%Y%m%d-%H%M%S).tar.gz"
tar -czf "$F" -C "$DATA" --exclude='state/secrets.env' --exclude='state/*.env' --exclude='state/locks' --exclude='state/keys' state
ls -1t "$OUT"/state-*.tar.gz | tail -n +15 | xargs -r rm -f
echo "[backup] $F"
