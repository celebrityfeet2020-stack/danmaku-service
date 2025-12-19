# coding=utf-8
"""
抖音直播弹幕抓取服务
基于 DouyinLiveRecorder 项目实现
"""
import _thread
import gzip
import hashlib
import json
import logging
import os
import random
import re
import threading
import time
from typing import Optional
from urllib.parse import urlparse, parse_qs, urlunparse, urlencode

import requests
import websocket
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from google.protobuf import json_format
from pydantic import BaseModel

# 导入protobuf定义
from dy_pb2 import PushFrame, Response, ChatMessage

# 尝试导入JS引擎
try:
    from py_mini_racer import MiniRacer
    JS_ENGINE = "py_mini_racer"
except ImportError:
    try:
        import js2py
        JS_ENGINE = "js2py"
    except ImportError:
        JS_ENGINE = None

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('danmaku-service')
logger.info(f"Using {JS_ENGINE} for JS execution")

app = FastAPI(title="Danmaku Service")

# CORS配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 全局变量
current_fetcher: Optional['DanmakuFetcher'] = None
fetcher_lock = threading.Lock()

# 请求模型
class StartRequest(BaseModel):
    live_id: str
    callback_url: str
    proxy: Optional[str] = None

class StopRequest(BaseModel):
    live_id: str

# 工具函数
def get_ms_stub(live_room_real_id, user_unique_id):
    """生成签名所需的stub"""
    params = {
        "live_id": "1",
        "aid": "6383",
        "version_code": 180800,
        "webcast_sdk_version": '1.0.14-beta.0',
        "room_id": live_room_real_id,
        "sub_room_id": "",
        "sub_channel_id": "",
        "did_rule": "3",
        "user_unique_id": user_unique_id,
        "device_platform": "web",
        "device_type": "",
        "ac": "",
        "identity": "audience"
    }
    sig_params = ','.join([f'{k}={v}' for k, v in params.items()])
    return hashlib.md5(sig_params.encode()).hexdigest()

def build_request_url(url: str, user_agent: str) -> str:
    """构建请求URL"""
    parsed_url = urlparse(url)
    existing_params = parse_qs(parsed_url.query)
    existing_params['aid'] = ['6383']
    existing_params['device_platform'] = ['web']
    existing_params['browser_language'] = ['zh-CN']
    existing_params['browser_platform'] = ['Win32']
    existing_params['browser_name'] = [user_agent.split('/')[0]]
    existing_params['browser_version'] = [
        user_agent.split(existing_params['browser_name'][0])[-1][1:]]
    new_query_string = urlencode(existing_params, doseq=True)
    new_url = urlunparse((
        parsed_url.scheme,
        parsed_url.netloc,
        parsed_url.path,
        parsed_url.params,
        new_query_string,
        parsed_url.fragment
    ))
    return new_url

def get_request_headers():
    """获取请求头"""
    return {
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'referer': 'https://live.douyin.com/',
        'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'accept-language': 'zh-CN,zh;q=0.9',
    }

class DanmakuFetcher:
    """弹幕抓取器"""
    
    def __init__(self, live_id: str, callback_url: str, proxy: Optional[str] = None):
        self.live_id = live_id
        self.callback_url = callback_url
        self.proxy = proxy
        self.room_id = None
        self.room_real_id = None
        self.ttwid = None
        self.user_agent = get_request_headers()['user-agent']
        self.ws = None
        self.stop_signal = False
        self._running = False
        self._connected = False
        self.danmu_count = 0
        self.user_unique_id = random.randint(7300000000000000000, 7999999999999999999)
        
        # 加载JS引擎
        self.js_ctx = None
        self._init_js_engine()
    
    def _init_js_engine(self):
        """初始化JS引擎"""
        js_path = os.path.join(os.path.dirname(__file__), 'webmssdk.js')
        if not os.path.exists(js_path):
            logger.warning("webmssdk.js not found, signature will be empty")
            return
        
        try:
            with open(js_path, 'r', encoding='utf-8') as f:
                js_code = f.read()
            
            js_dom = f"""
document = {{}}
window = {{}}
navigator = {{
  'userAgent': '{self.user_agent}'
}}
""".strip()
            
            if JS_ENGINE == "py_mini_racer":
                self.js_ctx = MiniRacer()
                self.js_ctx.eval(js_dom + js_code)
            elif JS_ENGINE == "js2py":
                self.js_ctx = js2py.EvalJs()
                self.js_ctx.execute(js_dom + js_code)
            
            logger.info("JS engine initialized successfully")
        except Exception as e:
            logger.error(f"Failed to init JS engine: {e}")
            self.js_ctx = None
    
    def _get_signature(self, room_real_id):
        """生成签名"""
        if not self.js_ctx:
            return ""
        
        try:
            ms_stub = get_ms_stub(room_real_id, self.user_unique_id)
            if JS_ENGINE == "py_mini_racer":
                signature = self.js_ctx.eval(f"get_sign('{ms_stub}')")
            elif JS_ENGINE == "js2py":
                signature = self.js_ctx.get_sign(ms_stub)
            else:
                signature = ""
            return signature
        except Exception as e:
            logger.error(f"Failed to generate signature: {e}")
            return ""
    
    def _get_ttwid(self):
        """获取ttwid"""
        try:
            proxies = {'http': self.proxy, 'https': self.proxy} if self.proxy else None
            resp = requests.get(
                'https://live.douyin.com/',
                headers=get_request_headers(),
                proxies=proxies,
                timeout=10
            )
            cookies = resp.cookies.get_dict()
            self.ttwid = cookies.get('ttwid', '')
            if self.ttwid:
                logger.info(f"Got ttwid: {self.ttwid[:30]}...")
            return self.ttwid
        except Exception as e:
            logger.error(f"Failed to get ttwid: {e}")
            return None
    
    def _get_room_info(self):
        """获取直播间信息"""
        try:
            proxies = {'http': self.proxy, 'https': self.proxy} if self.proxy else None
            url = f'https://live.douyin.com/{self.live_id}'
            resp = requests.get(
                url,
                headers={**get_request_headers(), 'cookie': f'ttwid={self.ttwid}'},
                proxies=proxies,
                timeout=10
            )
            
            # 尝试多种模式匹配room_id
            patterns = [
                r'roomId\\":\\"(\d+)\\"',
                r'"roomId":"(\d+)"',
                r'room_id["\':]+(\d+)',
                r'id_str\\":\\"(\d+)\\"',
                r'"id_str":"(\d+)"',
            ]
            
            for pattern in patterns:
                match = re.search(pattern, resp.text)
                if match:
                    self.room_real_id = match.group(1)
                    logger.info(f"Got room_real_id: {self.room_real_id} (pattern: {pattern[:30]}...)")
                    return self.room_real_id
            
            logger.error("Failed to get room_real_id from page")
            return None
        except Exception as e:
            logger.error(f"Failed to get room info: {e}")
            return None
    
    def _get_ws_url(self):
        """获取WebSocket URL"""
        signature = self._get_signature(self.room_real_id)
        
        webcast_params = {
            "room_id": self.room_real_id,
            "compress": 'gzip',
            "version_code": 180800,
            "webcast_sdk_version": '1.0.14-beta.0',
            "live_id": "1",
            "did_rule": "3",
            "user_unique_id": self.user_unique_id,
            "identity": "audience",
            "signature": signature,
        }
        
        base_url = f"wss://webcast5-ws-web-lf.douyin.com/webcast/im/push/v2/?{'&'.join([f'{k}={v}' for k, v in webcast_params.items()])}"
        return build_request_url(base_url, self.user_agent)
    
    def start(self):
        """启动弹幕抓取"""
        self._running = True
        self.stop_signal = False
        
        # 获取ttwid
        if not self._get_ttwid():
            logger.error("Failed to get ttwid")
            self._running = False
            return
        
        # 获取房间信息
        if not self._get_room_info():
            logger.error("Failed to get room info")
            self._running = False
            return
        
        # 连接WebSocket
        self._connect_websocket()
    
    def _connect_websocket(self):
        """连接WebSocket"""
        ws_url = self._get_ws_url()
        logger.info(f"Connecting to WebSocket: {ws_url[:100]}...")
        
        headers = {
            "cookie": f"ttwid={self.ttwid}",
            'user-agent': self.user_agent,
        }
        
        # 配置代理
        proxy_type = None
        http_proxy_host = None
        http_proxy_port = None
        
        if self.proxy:
            parsed = urlparse(self.proxy)
            if parsed.scheme in ('socks5', 'socks5h'):
                proxy_type = 'socks5'
            elif parsed.scheme in ('http', 'https'):
                proxy_type = 'http'
            http_proxy_host = parsed.hostname
            http_proxy_port = parsed.port
            logger.info(f"Using proxy: {proxy_type}://{http_proxy_host}:{http_proxy_port}")
        
        self.ws = websocket.WebSocketApp(
            ws_url,
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
        finally:
            self._running = False
            self._connected = False
    
    def _on_ws_open(self, ws):
        """WebSocket连接打开"""
        logger.info(f"WebSocket connected for live_id: {self.live_id}")
        self._connected = True
        logger.info("Starting heartbeat thread...")
        # 启动心跳线程
        _thread.start_new_thread(self._heartbeat, (ws,))
    
    def _on_ws_message(self, ws: websocket.WebSocketApp, message: bytes):
        """处理WebSocket消息"""
        logger.info(f"Received WS message, length: {len(message)} bytes")
        try:
            # 解析PushFrame
            push_frame = PushFrame()
            push_frame.ParseFromString(message)
            logid = push_frame.logid
            logger.info(f"PushFrame parsed, logid: {logid}, payload length: {len(push_frame.payload)}")
            
            # 解压payload
            decompressed = gzip.decompress(push_frame.payload)
            logger.info(f"Decompressed payload length: {len(decompressed)}")
            
            # 解析Response
            response = Response()
            response.ParseFromString(decompressed)
            logger.info(f"Response parsed, needAck: {response.needAck}, messages count: {len(response.messagesList)}")
            
            # 发送ACK
            if response.needAck:
                ack_frame = PushFrame()
                ack_frame.payloadType = 'ack'
                ack_frame.logid = logid
                ack_frame.payloadType = response.internalExt
                ack_data = ack_frame.SerializeToString()
                ws.send(ack_data, websocket.ABNF.OPCODE_BINARY)
                logger.info("ACK sent")
            
            # 处理消息
            for msg in response.messagesList:
                logger.info(f"Message method: {msg.method}")
                if msg.method == 'WebcastChatMessage':
                    self._handle_chat_message(msg.payload)
                    
        except Exception as e:
            logger.error(f"Error processing message: {e}")
    
    def _handle_chat_message(self, payload: bytes):
        """处理聊天消息"""
        try:
            chat_msg = ChatMessage()
            chat_msg.ParseFromString(payload)
            data = json_format.MessageToDict(chat_msg, preserving_proto_field_name=True)
            
            user = data.get('user', {}).get('nickName', 'Unknown')
            content = data.get('content', '')
            
            self.danmu_count += 1
            logger.info(f"[弹幕] {user}: {content}")
            
            # 推送到回调URL
            self._push_danmu(user, content)
            
        except Exception as e:
            logger.error(f"Error handling chat message: {e}")
    
    def _push_danmu(self, user: str, content: str, user_id: str = ""):
        """推送弹幕到回调URL"""
        logger.info(f"Pushing danmu to {self.callback_url}: {user}: {content}")
        try:
            from datetime import datetime
            payload = {
                "live_id": self.live_id,
                "type": "chat",
                "user_id": user_id or str(hash(user) % 10000000000),  # 生成一个伪user_id
                "nickname": user,
                "content": content,
                "timestamp": datetime.now().isoformat(),
                "extra": {}
            }
            
            logger.info(f"Push payload: {payload}")
            resp = requests.post(
                self.callback_url,
                json=payload,
                headers={'Content-Type': 'application/json'},
                timeout=5
            )
            
            logger.info(f"Push response: {resp.status_code} - {resp.text[:200] if resp.text else 'empty'}")
            if resp.status_code != 200:
                logger.warning(f"Failed to push danmu: {resp.status_code}")
                
        except Exception as e:
            logger.error(f"Error pushing danmu: {e}")
    
    def _heartbeat(self, ws: websocket.WebSocketApp):
        """心跳线程"""
        logger.info("Heartbeat thread started")
        while not self.stop_signal and self._running:
            time.sleep(10)
            if self.stop_signal or not self._running:
                break
            try:
                # 发送心跳包
                obj = PushFrame()
                obj.payloadType = 'hb'
                data = obj.SerializeToString()
                ws.send(data, websocket.ABNF.OPCODE_BINARY)
                logger.info("Heartbeat sent")
            except Exception as e:
                logger.error(f"Heartbeat error: {e}")
                break
        
        logger.info("Heartbeat thread stopped")
    
    def _on_ws_error(self, ws, error):
        """WebSocket错误"""
        logger.error(f"WebSocket error: {error}")
    
    def _on_ws_close(self, ws, close_status_code, close_msg):
        """WebSocket关闭"""
        logger.info(f"WebSocket closed: {close_status_code} - {close_msg}")
        self._connected = False
    
    def stop(self):
        """停止弹幕抓取"""
        self.stop_signal = True
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


# API端点
@app.get("/")
async def root():
    return {"status": "ok", "service": "danmaku-service"}

@app.get("/status")
async def status():
    global current_fetcher
    with fetcher_lock:
        if current_fetcher and current_fetcher.is_running():
            return {
                "running": True,
                "live_id": current_fetcher.live_id,
                "danmu_count": current_fetcher.danmu_count
            }
        return {"running": False}

@app.post("/start")
async def start_service(request: StartRequest):
    global current_fetcher
    
    with fetcher_lock:
        # 如果已有服务在运行，先停止
        if current_fetcher and current_fetcher.is_running():
            logger.info(f"Stopping old service for live_id: {current_fetcher.live_id}")
            current_fetcher.stop()
            time.sleep(1)
        
        # 创建新的抓取器
        current_fetcher = DanmakuFetcher(
            live_id=request.live_id,
            callback_url=request.callback_url,
            proxy=request.proxy
        )
        
        # 在后台线程启动
        thread = threading.Thread(target=current_fetcher.start, daemon=True)
        thread.start()
        
        logger.info(f"Started fetcher for live_id: {request.live_id}")
        
    return {"success": True, "message": f"Started for live_id: {request.live_id}"}

@app.post("/stop")
async def stop_service(request: StopRequest = None):
    global current_fetcher
    
    with fetcher_lock:
        if current_fetcher:
            current_fetcher.stop()
            live_id = current_fetcher.live_id
            current_fetcher = None
            return {"success": True, "message": f"Stopped for live_id: {live_id}"}
        return {"success": False, "message": "No service running"}

@app.post("/stop/{live_id}")
async def stop_service_by_id(live_id: str):
    global current_fetcher
    
    with fetcher_lock:
        if current_fetcher and current_fetcher.live_id == live_id:
            current_fetcher.stop()
            current_fetcher = None
            return {"success": True, "message": f"Stopped for live_id: {live_id}"}
        return {"success": False, "message": f"No service running for live_id: {live_id}"}


if __name__ == "__main__":
    import uvicorn
    print("=== Starting Danmaku Service ===")
    uvicorn.run(app, host="0.0.0.0", port=16888)
