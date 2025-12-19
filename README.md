# 弹幕抓取服务 (Danmaku Service)

抖音直播弹幕抓取服务，用于Agent7数字人直播系统。

## 功能

- 通过WireGuard隧道连接VPS9代理出口
- 抓取抖音直播间弹幕
- 实时推送到Agent7后端

## 架构

```
抖音直播间 ← VPS9(代理出口) ← WireGuard隧道 ← D5弹幕服务容器 → 推送到M3/Agent7
```

## API

### 启动弹幕抓取
```bash
POST /start
{
    "live_id": "586086640170",
    "proxy": "socks5://10.100.100.1:1080",
    "agent7_url": "http://192.168.9.125:12777/api/danmaku/receive"
}
```

### 停止弹幕抓取
```bash
POST /stop
```

### 获取状态
```bash
GET /status
```

## 部署

### Docker运行
```bash
docker pull junpeng999/danmaku-service:latest
docker run -d \
    --name danmaku-service \
    --cap-add=NET_ADMIN \
    --cap-add=SYS_MODULE \
    --sysctl net.ipv4.conf.all.src_valid_mark=1 \
    -p 16888:16888 \
    junpeng999/danmaku-service:latest
```

### Docker Compose
```bash
docker compose up -d
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| SOCKS5_PROXY | socks5://10.100.100.1:1080 | SOCKS5代理地址 |
| AGENT7_API_URL | http://192.168.9.125:12777/api/danmaku/receive | Agent7弹幕接收API |
| LIVE_ID | - | 自动启动的直播间ID |

## 版本

- v1.0 - 初始版本
