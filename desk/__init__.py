"""ta-desk：基于 TradingAgents 的多智能体加密货币纸面交易台。"""
# 2b：任何 desk 模块加载前先清除实盘 OKX 凭据环境变量（只按变量名，不读值），之后才可能 import /workspace/ai-trader。
from desk import envguard as _envguard  # noqa: E402,F401
