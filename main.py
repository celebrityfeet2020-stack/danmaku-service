#!/usr/bin/env python3
"""
抖音弹幕抓取服务
- 通过SOCKS5代理连接抖音直播间
- 抓取弹幕后推送到Agent7后端
"""

import os
import sys
import json
import time
import queue
import hashlib
import random
import string
import codecs
import logging
import threading
import re
import gzip
from typing import Optional, Callable, Dict, Any
from dataclasses import dataclass, asdict
from datetime import datetime

import requests
import websocket

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('danmaku-service')

# 环境变量配置
SOCKS5_PROXY = os.getenv('SOCKS5_PROXY', 'socks5://10.100.100.1:1080')
AGENT7_API_URL = os.getenv('AGENT7_API_URL', 'http://192.168.9.125:12777/api/danmaku/receive')
LIVE_ID = os.getenv('LIVE_ID', '')

# 尝试导入py_mini_racer，如果失败则使用execjs
try:
    from py_mini_racer import MiniRacer
    USE_MINI_RACER = True
    logger.info("Using py_mini_racer for JS execution")
except ImportError:
    try:
        import execjs
        USE_MINI_RACER = False
        logger.info("Using execjs for JS execution")
    except ImportError:
        logger.error("Neither py_mini_racer nor execjs available!")
        USE_MINI_RACER = None


@dataclass
class DanmuMessage:
    """弹幕消息数据类"""
    type: str  # 消息类型: chat, gift, like, enter, follow
    nickname: str  # 用户昵称
    content: str  # 消息内容
    user_id: str = ""  # 用户ID
    avatar: str = ""  # 头像URL
    level: int = 0  # 用户等级
    timestamp: float = 0  # 时间戳
    raw_data: Dict = None  # 原始数据


class DouyinDanmuFetcher:
    """抖音弹幕抓取器"""
    
    def __init__(
        self,
        live_id: str,
        proxy: str = None,
        on_message: Callable[[DanmuMessage], None] = None,
        on_connect: Callable[[str], None] = None,
        on_disconnect: Callable[[str], None] = None,
        on_error: Callable[[str, Exception], None] = None,
    ):
        self.live_id = live_id
        self.proxy = proxy
        
        # 回调函数
        self.on_message = on_message
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self.on_error = on_error
        
        # 消息队列
        self.message_queue: queue.Queue = queue.Queue(maxsize=1000)
        
        # 状态
        self._running = False
        self._connected = False
        self._thread: Optional[threading.Thread] = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        
        # 缓存
        self.__ttwid = None
        self.__room_id = None
        
        # HTTP会话
        self.session = requests.Session()
        if proxy:
            self.session.proxies = {
                'http': proxy,
                'https': proxy
            }
        
        # 配置
        self.host = "https://www.douyin.com/"
        self.live_url = "https://live.douyin.com/"
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
        self.headers = {'User-Agent': self.user_agent}
        
        # WebSocket
        self.ws: Optional[websocket.WebSocketApp] = None
        
        # JS目录
        self.js_dir = os.path.dirname(os.path.abspath(__file__))
    
    @property
    def ttwid(self) -> str:
        """获取ttwid cookie"""
        if self.__ttwid:
            return self.__ttwid
        try:
            response = self.session.get(self.live_url, headers=self.headers, timeout=10)
            response.raise_for_status()
            self.__ttwid = response.cookies.get('ttwid')
            logger.info(f"Got ttwid: {self.__ttwid[:20]}...")
            return self.__ttwid
        except Exception as e:
            logger.error(f"Failed to get ttwid: {e}")
            if self.on_error:
                self.on_error(self.live_id, e)
            return ""
    
    @property
    def room_id(self) -> str:
        """获取真实room_id"""
        if self.__room_id:
            return self.__room_id
        
        url = self.live_url + self.live_id
        headers = {
            "User-Agent": self.user_agent,
            "cookie": f"ttwid={self.ttwid}&msToken={self._generate_ms_token()}; __ac_nonce=0123407cc00a9e438deb4",
        }
        try:
            response = self.session.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            match = re.search(r'roomId\\":\\"(\d+)\\"', response.text)
            if match:
                self.__room_id = match.group(1)
                logger.info(f"Got room_id: {self.__room_id}")
                return self.__room_id
        except Exception as e:
            logger.error(f"Failed to get room_id: {e}")
            if self.on_error:
                self.on_error(self.live_id, e)
        return ""
    
    def _generate_ms_token(self, length: int = 182) -> str:
        """生成msToken"""
        chars = string.ascii_letters + string.digits + '-_'
        return ''.join(random.choice(chars) for _ in range(length))
    
    def _generate_signature(self, wss: str) -> str:
        """生成WebSocket签名"""
        import urllib.parse
        
        params = ("live_id,aid,version_code,webcast_sdk_version,"
                  "room_id,sub_room_id,sub_channel_id,did_rule,"
                  "user_unique_id,device_platform,device_type,ac,"
                  "identity").split(',')
        wss_params = urllib.parse.urlparse(wss).query.split('&')
        wss_maps = {i.split('=')[0]: i.split("=")[-1] for i in wss_params}
        tpl_params = [f"{i}={wss_maps.get(i, '')}" for i in params]
        param = ','.join(tpl_params)
        
        md5 = hashlib.md5()
        md5.update(param.encode())
        md5_param = md5.hexdigest()
        
        # 加载签名JS
        sign_js_path = os.path.join(self.js_dir, 'js', 'sign.js')
        if not os.path.exists(sign_js_path):
            sign_js_path = os.path.join(self.js_dir, 'sign.js')
        
        if not os.path.exists(sign_js_path):
            logger.warning(f"sign.js not found at {sign_js_path}, using empty signature")
            return ""
        
        try:
            with codecs.open(sign_js_path, 'r', encoding='utf8') as f:
                script = f.read()
            
            if USE_MINI_RACER:
                ctx = MiniRacer()
                ctx.eval(script)
                signature = ctx.call("get_sign", md5_param)
            elif USE_MINI_RACER is False:
                import execjs
                ctx = execjs.compile(script)
                signature = ctx.call("get_sign", md5_param)
            else:
                signature = ""
            
            return signature
        except Exception as e:
            logger.error(f"Failed to generate signature: {e}")
            if self.on_error:
                self.on_error(self.live_id, e)
            return ""
    
    def start(self):
        """启动弹幕抓取（非阻塞）"""
        if self._running:
            return
        
        self._running = True
        self._thread = threading.Thread(target=self._connect_websocket, daemon=True)
        self._thread.start()
        logger.info(f"Started fetcher for live_id: {self.live_id}")
    
    def stop(self):
        """停止弹幕抓取"""
        self._running = False
        if self.ws:
            try:
                self.ws.close()
            except:
                pass
        self._connected = False
        logger.info(f"Stopped fetcher for live_id: {self.live_id}")
    
    def is_running(self) -> bool:
        """是否正在运行"""
        return self._running and self._connected
    
    def _connect_websocket(self):
        """连接WebSocket"""
        if not self.room_id:
            logger.error("Failed to get room_id, cannot connect")
            self._running = False
            return
        
        wss = (
            f"wss://webcast100-ws-web-lq.douyin.com/webcast/im/push/v2/?app_name=douyin_web"
            f"&version_code=180800&webcast_sdk_version=1.0.14-beta.0"
            f"&update_version_code=1.0.14-beta.0&compress=gzip&device_platform=web&cookie_enabled=true"
            f"&screen_width=1536&screen_height=864&browser_language=zh-CN&browser_platform=Win32"
            f"&browser_name=Mozilla"
            f"&browser_version=5.0%20(Windows%20NT%2010.0;%20Win64;%20x64)%20AppleWebKit/537.36%20(KHTML,"
            f"%20like%20Gecko)%20Chrome/126.0.0.0%20Safari/537.36"
            f"&browser_online=true&tz_name=Asia/Shanghai"
            f"&cursor=d-1_u-1_fh-7392091211001140287_t-1721106114633_r-1"
            f"&internal_ext=internal_src:dim|wss_push_room_id:{self.room_id}|wss_push_did:7319483754668557238"
            f"|first_req_ms:1721106114541|fetch_time:1721106114633|seq:1|wss_info:0-1721106114633-0-0|"
            f"wrds_v:7392094459690748497"
            f"&host=https://live.douyin.com&aid=6383&live_id=1&did_rule=3&endpoint=live_pc&support_wrds=1"
            f"&user_unique_id=7319483754668557238&im_path=/webcast/im/fetch/&identity=audience"
            f"&need_persist_msg_count=15&insert_task_id=&live_reason=&room_id={self.room_id}&heartbeatDuration=0"
        )
        
        signature = self._generate_signature(wss)
        if signature:
            wss += f"&signature={signature}"
        
        headers = {
            "cookie": f"ttwid={self.ttwid}",
            'user-agent': self.user_agent,
        }
        
        # 配置代理
        proxy_type = None
        http_proxy_host = None
        http_proxy_port = None
        
        if self.proxy:
            # 解析代理URL: socks5://host:port
            import urllib.parse
            parsed = urllib.parse.urlparse(self.proxy)
            if parsed.scheme in ('socks5', 'socks5h'):
                proxy_type = 'socks5'
            elif parsed.scheme in ('http', 'https'):
                proxy_type = 'http'
            http_proxy_host = parsed.hostname
            http_proxy_port = parsed.port
            logger.info(f"Using proxy: {proxy_type}://{http_proxy_host}:{http_proxy_port}")
        
        self.ws = websocket.WebSocketApp(
            wss,
            header=headers,
            on_open=self._on_ws_open,
            on_message=self._on_ws_message,
            on_error=self._on_ws_error,
            on_close=self._on_ws_close
        )
        
        try:
            self.ws.run_forever(
                proxy_type=proxy_type,
                http_proxy_host=http_proxy_host,
                http_proxy_port=http_proxy_port
            )
        except Exception as e:
            logger.error(f"WebSocket error: {e}")
            if self.on_error:
                self.on_error(self.live_id, e)
        finally:
            self._running = False
            self._connected = False
    
    def _on_ws_open(self, ws):
        """WebSocket连接成功"""
        self._connected = True
        logger.info(f"WebSocket connected for live_id: {self.live_id}")
        
        # 启动心跳线程
        self._heartbeat_thread = threading.Thread(target=self._send_heartbeat, daemon=True)
        self._heartbeat_thread.start()
        
        if self.on_connect:
            self.on_connect(self.live_id)
    
    def _on_ws_message(self, ws, message):
        """收到WebSocket消息"""
        try:
            # 尝试解压gzip
            try:
                message = gzip.decompress(message)
            except:
                pass
            
            # 解析消息（简化版，实际需要protobuf解析）
            # 这里使用正则匹配提取弹幕内容
            text = message.decode('utf-8', errors='ignore')
            
            # 匹配聊天消息
            chat_pattern = r'WebcastChatMessage.*?nickname[^\x00-\x1f]*?([^\x00-\x1f]{2,20})[^\x00-\x1f]*?content[^\x00-\x1f]*?([^\x00-\x1f]{1,200})'
            matches = re.findall(chat_pattern, text)
            
            for match in matches:
                if len(match) >= 2:
                    nickname = match[0].strip()
                    content = match[1].strip()
                    
                    if nickname and content and len(content) > 0:
                        msg = DanmuMessage(
                            type='chat',
                            nickname=nickname,
                            content=content,
                            timestamp=time.time()
                        )
                        
                        logger.info(f"弹幕: {nickname}: {content}")
                        
                        if self.on_message:
                            self.on_message(msg)
                        
                        try:
                            self.message_queue.put_nowait(msg)
                        except queue.Full:
                            pass
        except Exception as e:
            logger.debug(f"Failed to parse message: {e}")
    
    def _on_ws_error(self, ws, error):
        """WebSocket错误"""
        logger.error(f"WebSocket error: {error}")
        if self.on_error:
            self.on_error(self.live_id, error)
    
    def _on_ws_close(self, ws, close_status_code, close_msg):
        """WebSocket关闭"""
        self._connected = False
        logger.info(f"WebSocket closed: {close_status_code} - {close_msg}")
        if self.on_disconnect:
            self.on_disconnect(self.live_id)
    
    def _send_heartbeat(self):
        """发送心跳包"""
        while self._running and self._connected:
            try:
                # 发送心跳
                if self.ws:
                    self.ws.send(b'\x3a\x02\x68\x62', opcode=websocket.ABNF.OPCODE_BINARY)
            except Exception as e:
                logger.debug(f"Heartbeat error: {e}")
            time.sleep(10)


class DanmakuService:
    """弹幕服务主类"""
    
    def __init__(self, live_id: str, proxy: str, agent7_url: str, thread_id: str = ""):
        self.live_id = live_id
        self.proxy = proxy
        self.agent7_url = agent7_url
        self.thread_id = thread_id  # A7会话ID
        self.fetcher: Optional[DouyinDanmuFetcher] = None
        self.running = False
        self.message_count = 0
        self.push_count = 0
    
    def on_message(self, msg: DanmuMessage):
        """收到弹幕消息"""
        self.message_count += 1
        
        # 推送到Agent7
        try:
            data = {
                'live_id': self.live_id,
                'thread_id': self.thread_id,  # A7会话ID
                'type': msg.type,
                'nickname': msg.nickname,
                'content': msg.content,
                'user_id': msg.user_id,
                'timestamp': msg.timestamp,
            }
            
            response = requests.post(
                self.agent7_url,
                json=data,
                timeout=5
            )
            
            if response.status_code == 200:
                self.push_count += 1
                logger.debug(f"Pushed message to Agent7: {msg.nickname}: {msg.content}")
            else:
                logger.warning(f"Failed to push to Agent7: {response.status_code}")
        except Exception as e:
            logger.error(f"Failed to push to Agent7: {e}")
    
    def on_connect(self, live_id: str):
        """连接成功"""
        logger.info(f"Connected to live room: {live_id}")
    
    def on_disconnect(self, live_id: str):
        """断开连接"""
        logger.info(f"Disconnected from live room: {live_id}")
        
        # 自动重连
        if self.running:
            logger.info("Reconnecting in 5 seconds...")
            time.sleep(5)
            self.start()
    
    def on_error(self, live_id: str, error: Exception):
        """错误处理"""
        logger.error(f"Error in live room {live_id}: {error}")
    
    def start(self):
        """启动服务"""
        self.running = True
        
        self.fetcher = DouyinDanmuFetcher(
            live_id=self.live_id,
            proxy=self.proxy,
            on_message=self.on_message,
            on_connect=self.on_connect,
            on_disconnect=self.on_disconnect,
            on_error=self.on_error,
        )
        
        self.fetcher.start()
        logger.info(f"Danmaku service started for live_id: {self.live_id}")
    
    def stop(self):
        """停止服务"""
        self.running = False
        if self.fetcher:
            self.fetcher.stop()
        logger.info("Danmaku service stopped")
    
    def get_status(self) -> dict:
        """获取状态"""
        return {
            'live_id': self.live_id,
            'running': self.running,
            'connected': self.fetcher.is_running() if self.fetcher else False,
            'message_count': self.message_count,
            'push_count': self.push_count,
        }


# FastAPI服务
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn

app = FastAPI(title="弹幕抓取服务", version="1.0.0")

# 全局服务实例
service: Optional[DanmakuService] = None


class StartRequest(BaseModel):
    live_id: str
    proxy: str = SOCKS5_PROXY
    agent7_url: str = AGENT7_API_URL
    thread_id: str = ""  # A7会话ID


class StopRequest(BaseModel):
    pass


@app.get("/")
async def root():
    return {"service": "danmaku-service", "status": "running"}


@app.get("/status")
async def get_status():
    global service
    if service:
        return service.get_status()
    return {"running": False}


@app.post("/start")
async def start_service(req: StartRequest):
    global service
    
    # 如果已有服务在运行，先停止它
    if service and service.running:
        old_live_id = service.live_id
        service.stop()
        logger.info(f"Stopped old service for live_id: {old_live_id}")
    
    service = DanmakuService(
        live_id=req.live_id,
        proxy=req.proxy,
        agent7_url=req.agent7_url,
        thread_id=req.thread_id,
    )
    service.start()
    
    return {"success": True, "message": f"Started for live_id: {req.live_id}", "thread_id": req.thread_id}


@app.post("/stop")
async def stop_service():
    global service
    
    if not service or not service.running:
        return {"success": False, "message": "Service not running"}
    
    service.stop()
    return {"success": True, "message": "Service stopped"}


@app.post("/stop/{live_id}")
async def stop_service_by_id(live_id: str):
    """根据live_id停止服务"""
    global service
    
    if not service or not service.running:
        return {"success": False, "message": "Service not running"}
    
    if service.live_id != live_id:
        return {"success": False, "message": f"Live ID mismatch: {service.live_id} != {live_id}"}
    
    service.stop()
    return {"success": True, "message": f"Service stopped for live_id: {live_id}"}


if __name__ == "__main__":
    # 如果设置了LIVE_ID环境变量，自动启动
    if LIVE_ID:
        logger.info(f"Auto-starting with LIVE_ID: {LIVE_ID}")
        service = DanmakuService(
            live_id=LIVE_ID,
            proxy=SOCKS5_PROXY,
            agent7_url=AGENT7_API_URL,
        )
        service.start()
    
    # 启动API服务
    uvicorn.run(app, host="0.0.0.0", port=16888)
