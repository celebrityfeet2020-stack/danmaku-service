FROM python:3.11-slim

# 安装WireGuard和必要工具
RUN apt-get update && apt-get install -y \
    wireguard-tools \
    iproute2 \
    iptables \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 安装Python依赖
RUN pip install --no-cache-dir \
    requests \
    websocket-client \
    pysocks \
    python-socks[asyncio] \
    fastapi \
    uvicorn \
    py_mini_racer \
    protobuf

# 复制代码
COPY main.py .
COPY dy_pb2.py .
COPY webmssdk.js .
COPY entrypoint.sh .

# 复制WireGuard配置
COPY wg-danmaku.conf /etc/wireguard/

# 设置权限
RUN chmod +x entrypoint.sh

# 暴露端口
EXPOSE 16888

# 启动脚本
ENTRYPOINT ["/app/entrypoint.sh"]
