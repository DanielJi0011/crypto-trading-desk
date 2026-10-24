"""离机备份上传（sftp）与离机心跳（HTTP POST）。2026-10-23 用户批准接到 <BACKUP_HOST>。

- upload(path)：用 sftp 批处理把文件传到 offsite.sftp_dir；只用 sftp，不用 rsync；私钥路径与已固定的主机指纹文件见 config.yaml offsite。
  失败记事件 offsite_backup_failed 并通知（只告警，不影响日报）。远端自动删除 30 天前文件，本地不做远端清理。
- heartbeat()：从 offsite.heartbeat_url_file 读 URL（含令牌），POST 一次，期望 204。URL 不打印、不记日志、不入库；
  文件缺失或返回非 204 时记事件 offsite_heartbeat_failed（不含 URL），连续失败 1 小时内只通知一次。
本模块从不输出私钥或 URL 内容。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from desk import settings

DEF = {"enabled": False, "sftp_host": "", "sftp_port": 22, "sftp_user": "", "sftp_dir": "/uploads",
       "sftp_key": "state/keys/ta_backup_ed25519", "known_hosts": "state/keys/known_hosts_154",
       "heartbeat_enabled": False, "heartbeat_minutes": 5, "heartbeat_url_file": "/home/box/.ta-desk/heartbeat_url",
       "heartbeat_url_fallback": "state/keys/heartbeat_url"}
HB_STATE = settings.STATE / "offsite_heartbeat.json"


def ocfg(cfg=None) -> dict:
    d = dict(DEF)
    d.update(((cfg if cfg is not None else settings.load_config()) or {}).get("offsite") or {})
    return d


def _abs(p: str) -> str:
    return p if os.path.isabs(p) else str(settings.ROOT / p)


def _alert(kind: str, message: str, notifier=None):
    from desk import okx_demo as X
    X.log_event(kind, message=message)
    if notifier is None:
        from desk.demo_ws import notify as notifier
    try:
        notifier({"kind": kind, "message": message})
    except Exception:
        pass


def upload(path: str, cfg=None, notifier=None, runner=subprocess.run) -> bool:
    c = ocfg(cfg)
    if not c["enabled"]:
        return False
    name = Path(path).name
    batch = f"put {path} {c['sftp_dir'].rstrip('/')}/{name}.part\nrename {c['sftp_dir'].rstrip('/')}/{name}.part {c['sftp_dir'].rstrip('/')}/{name}\n"
    cmd = [os.environ.get("TA_SFTP_BIN", "sftp"), "-b", "-", "-i", _abs(c["sftp_key"]), "-o", "IdentitiesOnly=yes",
           "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={_abs(c['known_hosts'])}",
           "-o", "ConnectTimeout=20", "-P", str(c["sftp_port"]), f"{c['sftp_user']}@{c['sftp_host']}"]
    try:
        r = runner(cmd, input=batch, capture_output=True, text=True, timeout=600)
        ok = r.returncode == 0
        why = f"sftp 退出码 {r.returncode}"
    except Exception as e:
        ok, why = False, type(e).__name__
    if ok:
        from desk import okx_demo as X
        X.log_event("offsite_backup_ok", file=name)
    else:
        _alert("offsite_backup_failed", f"离机备份上传失败：{name}（{why}），本地备份仍在，日报不受影响", notifier)
    return ok


def heartbeat(cfg=None, now: float | None = None, poster=None, notifier=None) -> int | None:
    """到间隔才发；返回 HTTP 状态码，未到时间或关闭返回 None，失败返回 0 或状态码。"""
    c = ocfg(cfg)
    if not c["heartbeat_enabled"]:
        return None
    now = float(now or time.time())
    st = settings.read_json(HB_STATE, {})
    if now - float(st.get("last_try", 0)) < float(c["heartbeat_minutes"]) * 60 - 20:
        return None
    st["last_try"] = now
    code = 0
    try:
        p = Path(c["heartbeat_url_file"])
        if not p.is_file() and c.get("heartbeat_url_fallback"):
            p = Path(_abs(c["heartbeat_url_fallback"]))   # 主路径缺失（如 /home/box 被重建）时读 state/keys 那份
        url = p.read_text(encoding="utf-8").strip()
        if poster is None:
            import requests
            poster = lambda u, b: requests.post(u, json=b, timeout=15).status_code
        code = int(poster(url, {"source": "ta-desk", "kind": "heartbeat", "time": settings.now_str()}))
    except Exception as e:
        st["last_error"] = type(e).__name__     # 只记异常类型，不记 URL
    st["last_code"] = code
    if code == 204:
        st["last_ok"] = now
        st.pop("alerted", None)
    else:
        from desk import okx_demo as X
        X.log_event("offsite_heartbeat_failed", code=code, error=st.get("last_error"))
        if "alerted" not in st or now - float(st["alerted"]) >= 3600:
            st["alerted"] = now
            _alert("offsite_heartbeat_failed", f"离机心跳失败（返回 {code}）", notifier)
    settings.write_json(HB_STATE, st)
    return code


if __name__ == "__main__":
    if sys.argv[1:2] == ["upload"]:
        sys.exit(0 if upload(sys.argv[2]) else 1)
    if sys.argv[1:2] == ["heartbeat"]:
        print(json.dumps({"code": heartbeat()}))
