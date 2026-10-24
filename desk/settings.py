"""配置、路径、时间与密钥读取（密钥绝不打印）。"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

CODE = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get("TA_DESK_HOME", CODE))          # 数据目录（state/reports/playbooks/logs）
CONFIG = Path(os.environ.get("TA_DESK_CONFIG", CODE / "config.yaml"))
STATE = ROOT / "state"
REPORTS = ROOT / "reports"
PLAYBOOKS = ROOT / "playbooks"
LOGS = ROOT / "logs"
TZ = ZoneInfo("Asia/Shanghai")
AI_TRADER = Path(os.environ.get("AI_TRADER_SRC", "/workspace/ai-trader"))

for p in (STATE, REPORTS, PLAYBOOKS, LOGS):
    p.mkdir(parents=True, exist_ok=True)

# ---------------- 紧急停止（R7）：全项目唯一路径常量，所有地方都经 kill_active()/halt_active() 判定
# KILL（停开仓）：禁止 open_now / place_trigger / 限价补单，撤开仓类条件单；允许平仓、减仓、收紧止损、到期平仓。
# HALT（全冻结）：除读取、通知外禁止一切自动写操作（okx_demo.post 层拦截）；交易所端已挂的止损止盈不动。
KILL_PATH = STATE / "KILL"
LEGACY_KILL_PATH = STATE / "ledger" / "KILL"    # 旧路径：过渡期内存在也视为生效，并提示迁移
HALT_PATH = STATE / "HALT"
_legacy_warned = False


def kill_active() -> bool:
    """KILL 是否生效（新路径 state/KILL，或过渡期旧路径 state/ledger/KILL）。HALT 不隐含在这里，见 halt_active。"""
    global _legacy_warned
    if KILL_PATH.exists():
        return True
    if LEGACY_KILL_PATH.exists():
        if not _legacy_warned:
            _legacy_warned = True
            print(f"[ta-desk] 警告：检测到旧路径 KILL 文件 {LEGACY_KILL_PATH}，已视为生效；请迁移到 {KILL_PATH}"
                  f"（mv {LEGACY_KILL_PATH} {KILL_PATH}）", file=sys.stderr, flush=True)
        return True
    return False


def halt_active() -> bool:
    return HALT_PATH.exists()


def no_new_positions() -> bool:
    """禁止开新仓：KILL 或 HALT 任一生效（HALT 比 KILL 更严）。"""
    return kill_active() or halt_active()


def config_hash(cfg: dict) -> str:
    """L1：配置指纹（排序后 JSON 的 sha256 前 12 位），写进每条决策记录，便于追溯当时用的参数。"""
    import hashlib
    import json as _j
    return hashlib.sha256(_j.dumps(cfg, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:12]


def load_config() -> dict:
    with open(CONFIG, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    from desk import envguard   # D1.4 启动检查：实盘 OKX 凭据变量必须已被清除
    envguard.scrub(); envguard.assert_clean()
    from desk import config_safety   # D1.4：安全配置未被代码执行 / 互相矛盾 → 启动失败
    config_safety.validate(cfg)
    llm = cfg["llm"]
    llm["deep_model"] = os.environ.get("TA_DEEP_MODEL") or llm["deep_model"]
    llm["quick_model"] = os.environ.get("TA_QUICK_MODEL") or llm["quick_model"]
    return cfg


def llm_base_url(cfg) -> str:
    return os.environ.get(cfg["llm"]["base_url_env"]) or cfg["llm"]["default_base_url"]


def llm_api_key(cfg) -> str | None:
    """返回密钥（仅用于传给客户端，调用方不得打印）。"""
    for name in [cfg["llm"]["api_key_env"], *cfg["llm"].get("fallback_key_envs", [])]:
        v = os.environ.get(name)
        if v:
            return v
    return None


def key_source(cfg) -> str:
    for name in [cfg["llm"]["api_key_env"], *cfg["llm"].get("fallback_key_envs", [])]:
        if os.environ.get(name):
            return name
    return "未设置"


def now() -> _dt.datetime:
    return _dt.datetime.now(TZ)


def now_str() -> str:
    return now().strftime("%Y-%m-%d %H:%M:%S%z")


SESSION = "morning"   # 当前场次（run_daily --session 设置），见 desk/sessions.py


def today() -> str:
    return now().strftime("%Y-%m-%d")


def read_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def write_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def append_jsonl(path: Path, rec: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def ai_trader_modules():
    """复用 /workspace/ai-trader 的 okx_client / guardrails / store。

    store 的路径由环境变量 AI_TRADER_HOME 决定 —— 指向 ta-desk 的独立账本目录，
    因此 guardrails 读取的是 ta-desk 的纸面交易记录，而不是 ai-trader 的。"""
    os.environ["AI_TRADER_HOME"] = str(STATE / "ledger")
    (STATE / "ledger" / "journal").mkdir(parents=True, exist_ok=True)
    from desk import envguard   # 2b：import ai-trader 之前再清一次并检查
    envguard.scrub(); envguard.assert_clean()
    if str(AI_TRADER) not in sys.path:
        sys.path.insert(0, str(AI_TRADER))
    import importlib
    import store  # noqa: E402
    if Path(store.HOME) != STATE / "ledger":   # 已被其他 HOME 导入过时强制重载
        store = importlib.reload(store)
    import guardrails  # noqa: E402
    import okx_client  # noqa: E402
    guardrails.store = store
    return okx_client, guardrails, store
