"""实盘凭据隔离（整改第一批 2b，配合 D1 第 4 点启动检查）。

任何 desk 模块被 import 时（desk/__init__.py）先清除进程环境中的实盘 OKX 凭据变量，保证随后 import /workspace/ai-trader
（okx_client.Credentials.from_env 读 OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSPHRASE）时拿不到实盘密钥。
判定规则：变量名以 OKX_ 开头、不以 OKX_DEMO_ 开头、且含 KEY / SECRET / PASS / TOKEN 之一。
只按变量名处理，绝不读取、打印或记录变量值。state/secrets.env 文件本身不改动。
"""
from __future__ import annotations

import os

_MARKERS = ("KEY", "SECRET", "PASS", "TOKEN")


class LiveCredentialError(RuntimeError):
    pass


def is_live_okx_var(name: str) -> bool:
    n = name.upper()
    return n.startswith("OKX_") and not n.startswith("OKX_DEMO_") and any(m in n for m in _MARKERS)


def live_vars() -> list[str]:
    return sorted(k for k in os.environ if is_live_okx_var(k))


def scrub() -> list[str]:
    """清除实盘 OKX 凭据变量，返回被清除的变量名（只有名字）。"""
    gone = live_vars()
    for k in gone:
        os.environ.pop(k, None)
    return gone


def assert_clean() -> None:
    """启动检查：清除后环境里仍有实盘变量 → 报错（只列变量名）。"""
    left = live_vars()
    if left:
        raise LiveCredentialError(f"环境中仍存在实盘 OKX 凭据变量 {left}，拒绝启动（禁止实盘）")


SCRUBBED = scrub()
