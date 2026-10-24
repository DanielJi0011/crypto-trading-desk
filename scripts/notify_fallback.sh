# 入口脚本共用的“兜底告警”（source 使用）：不依赖 Python，解释器缺失时也能告警。
# ta_alert <event_type> <中文消息> <日志文件>
#   1) 一定写 stderr 和日志；2) TA_NOTIFY_DISABLED=1 时不发 webhook；
#   3) 否则用 curl 走与 demo_ws.notify 相同的 webhook 通道（TA_NOTIFY_WEBHOOK_URL / _KEY，
#      环境变量缺失时从 $TA_ALERT_SECRETS 读取这两个变量）；URL/KEY 绝不打印。
ta_alert() {
  local kind="$1" msg="$2" log="${3:-/dev/null}" url="${TA_NOTIFY_WEBHOOK_URL:-}" key="${TA_NOTIFY_WEBHOOK_KEY:-}" sec="${TA_ALERT_SECRETS:-}"
  echo "[告警 $kind] $msg" | tee -a "$log" >&2
  if [ "${TA_NOTIFY_DISABLED:-0}" = "1" ]; then echo "[告警 $kind] 通知已禁用（TA_NOTIFY_DISABLED=1），仅记日志" >>"$log"; return 0; fi
  if [ -z "$url" ] && [ -n "$sec" ] && [ -f "$sec" ]; then
    url=$(sed -nE 's/^(export )?TA_NOTIFY_WEBHOOK_URL=//p' "$sec" | tail -1 | tr -d "\"'")
    [ -z "$key" ] && key=$(sed -nE 's/^(export )?TA_NOTIFY_WEBHOOK_KEY=//p' "$sec" | tail -1 | tr -d "\"'")
  fi
  if [ -z "$url" ] || ! command -v curl >/dev/null; then echo "[告警 $kind] webhook 通道不可用（未配置 URL 或无 curl），仅记日志与 stderr" | tee -a "$log" >&2; return 0; fi
  local esc t body; esc=$(printf '%s' "$msg" | sed 's/\\/\\\\/g; s/"/\\"/g'); t=$(TZ=Asia/Shanghai date "+%F %T")
  body="{\"event_type\":\"$kind\",\"message\":\"$esc\",\"time\":\"$t\",\"timezone\":\"Asia/Shanghai (北京时间)\",\"source\":\"run-script-fallback\"}"
  local hk=(); [ -n "$key" ] && hk=(-H "Authorization: Bearer $key" -H "X-Webhook-Key: $key")
  local code; code=$(curl -sS -o /dev/null -w '%{http_code}' -m 10 --retry 2 -H "Content-Type: application/json" "${hk[@]}" --data-binary "$body" "$url" 2>/dev/null) || code=fail
  echo "[告警 $kind] webhook 结果：$code" >>"$log"
  case "$code" in 2??) ;; *) echo "[告警 $kind] webhook 发送失败（$code），已写日志与 stderr" >&2 ;; esac
  return 0
}
