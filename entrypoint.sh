#!/bin/bash
set -e

echo "=== Danmaku Service Container Starting ==="

# 启动WireGuard
echo "Starting WireGuard..."
wg-quick up wg-danmaku

# 等待WireGuard接口就绪
sleep 2

# 检查WireGuard状态
echo "WireGuard status:"
wg show wg-danmaku

# 测试连接
echo "Testing connection to VPS9..."
ping -c 2 10.100.100.1 || echo "Warning: Cannot ping VPS9"

# 测试代理
echo "Testing SOCKS5 proxy..."
curl -x socks5://10.100.100.1:1080 -s --connect-timeout 5 https://httpbin.org/ip || echo "Warning: Proxy test failed"

echo "=== Starting Danmaku Service ==="

# 设置protobuf环境变量以兼容旧版本生成的pb2文件
export PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python

# 启动弹幕服务
exec python main.py
