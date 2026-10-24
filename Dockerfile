# ta-desk 交易台镜像（只连 OKX 模拟盘）。密钥、state/、reports/、logs/ 均不进镜像，运行时挂载。
FROM python:3.13-slim

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TA_DESK_PYTHON=/workspace/ta-desk/.venv/bin/python

RUN apt-get update \
 && apt-get install -y --no-install-recommends bash git curl ca-certificates tzdata procps util-linux openssh-client \
 && rm -rf /var/lib/apt/lists/*

# supercronic：容器内定时器（早晚场）
ARG SUPERCRONIC_VERSION=v0.2.33
RUN curl -fsSL -o /usr/local/bin/supercronic \
      "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-$(dpkg --print-architecture)" \
 && chmod +x /usr/local/bin/supercronic

# 工作目录与生产一致（部分脚本写死了 /workspace/ta-desk 路径）；脚本默认用 ./.venv/bin/python，所以在镜像里建同名 venv
WORKDIR /workspace/ta-desk
COPY requirements.txt .
RUN python -m venv .venv && .venv/bin/pip install -r requirements.txt

COPY . .
# vendor 子模块与生产相同：可编辑安装（克隆后需先 git submodule update --init）
RUN test -f vendor/TradingAgents/pyproject.toml || { echo "缺少 vendor/TradingAgents，请先 git submodule update --init" >&2; exit 1; } \
 && .venv/bin/pip install --no-deps -e vendor/TradingAgents \
 && mkdir -p state reports logs

CMD ["bash", "run_demo_ws.sh"]
