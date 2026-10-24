"""OKX v5 REST 封装 —— 【发布裁剪版】（ta-desk 公开仓库专用）。

与原 ai-trader/okx_client.py 相比，这个版本删除了：
  - 凭据类与读取实盘环境变量的逻辑（不读取任何 OKX 凭据环境变量）
  - 签名工具与请求头签名逻辑
  - 下单、撤单、设杠杆等全部写操作，以及余额、持仓、订单等全部私有接口
只保留公共行情接口（tickers / ticker / candles / history-candles / instruments）。
ta-desk 的模拟盘交易走 desk/okx_demo.py（只用 OKX_DEMO_* 密钥 + x-simulated-trading: 1），不经过本模块。
"""
from __future__ import annotations

import json
from typing import Any, Optional
from urllib.parse import urlencode

import requests

PUBLIC_PREFIXES = ("/api/v5/market/", "/api/v5/public/")


class OKXError(Exception):
    pass


class PrivateRequestRefused(OKXError):
    """发布裁剪版：禁止任何私有请求。"""


class OKXClient:
    def __init__(self, base_url: str = "https://www.okx.com", mode: Any = None, simulated: Any = None,
                 timeout: float = 10, credentials: Any = None, signal_writer: Any = None,
                 session: Optional[requests.Session] = None):
        # mode / simulated / credentials / signal_writer 仅为兼容旧调用方式，全部忽略
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.creds = None
        self.s = session or requests.Session()

    def __repr__(self) -> str:
        return "OKXClient(public-only)"

    def _request(self, method: str, path: str, params: Optional[dict] = None, body: Any = None,
                 private: bool = False) -> list:
        if private or method.upper() != "GET" or body is not None or not path.startswith(PUBLIC_PREFIXES):
            raise PrivateRequestRefused(f"发布裁剪版只允许公共行情 GET 请求，拒绝 {method} {path}")
        params = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
        request_path = path + (("?" + urlencode(params)) if params else "")
        r = self.s.request("GET", self.base_url + request_path, headers={"Content-Type": "application/json"},
                           timeout=self.timeout)
        try:
            j = r.json()
        except (ValueError, json.JSONDecodeError):
            raise OKXError(f"HTTP {r.status_code}：非 JSON 响应")
        if str(j.get("code")) != "0":
            raise OKXError(f"OKX 错误 code={j.get('code')} msg={j.get('msg')}")
        return j.get("data", [])

    # ------------------------------------------------------------ 公共行情
    def get_tickers(self, inst_type: str = "SPOT") -> list:
        return self._request("GET", "/api/v5/market/tickers", {"instType": inst_type})

    def get_ticker(self, inst_id: str) -> dict:
        d = self._request("GET", "/api/v5/market/ticker", {"instId": inst_id})
        return d[0] if d else {}

    def get_candles(self, inst_id: str, bar: str = "15m", limit: int = 100,
                    after: Optional[str] = None, before: Optional[str] = None) -> list:
        """近期 K 线（最多 300 根/次）。返回 [ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm]，新→旧。"""
        return self._request("GET", "/api/v5/market/candles",
                             {"instId": inst_id, "bar": bar, "limit": limit, "after": after, "before": before})

    def get_history_candles(self, inst_id: str, bar: str = "15m", limit: int = 100,
                            after: Optional[str] = None, before: Optional[str] = None) -> list:
        """历史 K 线（最多 100 根/次），用 after=更早的 ts 向前翻页。"""
        return self._request("GET", "/api/v5/market/history-candles",
                             {"instId": inst_id, "bar": bar, "limit": limit, "after": after, "before": before})

    def get_instruments(self, inst_type: str = "SPOT", inst_id: Optional[str] = None) -> list:
        return self._request("GET", "/api/v5/public/instruments", {"instType": inst_type, "instId": inst_id})
